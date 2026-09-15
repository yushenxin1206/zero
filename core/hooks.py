"""
Hooks in agent loop.

核心只定义契约与分发语义，不含任何策略；无插件注册时全部为 no-op，
运行时行为与无 hooks 时逐字节一致。设计方案见 docs/hooks.md。

hook 点位（①~⑥ 对应文档中的生命周期图）：
    ① before_turn          msg_in 出队后、持久化前          拦截+改写
    ② before_iteration     每次 LLM 调用前                  拦截+改写
    ③ after_llm_response   一轮流式输出结束后               观察+改写
    ④ before_tool_call     kwargs 解析后、tool.run 前       拦截+替换
    ⑤ before_tool_result   工具执行后、截断+持久化前        改写
    ⑥ before_msg_out       MessageOut 组装后、持久化前      改写+抑制

handler 一律 async；任何 handler 抛异常只记日志、视为 no-op（fail-open），
需要 fail-closed 的插件应在正常返回路径里返回否决决策。
"""
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from datacls import LLMPayload, MessageIn, MessageOut, ToolCall, ToolContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

@dataclass
class HookContext:
    """所有 hook 共用的运行时上下文，一次构建、逐 hook 透传。"""
    channel: str
    session_id: str
    turn_id: str = ""                       # 触发本轮的 MessageIn.id（runner 侧可能为空）
    iteration: int = 0                      # 仅 iteration 级 hook 有意义
    tool_context: ToolContext | None = None
    metadata: dict[str, Any] = field(default_factory=dict)  # 复制自 msg_in.metadata


# ---------------------------------------------------------------------------
# Decisions —— handler 返回 None 一律等价于"不干预"
# ---------------------------------------------------------------------------

@dataclass
class TurnDecision:
    """① before_turn 的决策。"""
    proceed: bool = True
    reason: str = ""                        # 拒绝时可选：作为一条普通回复发回
    silent: bool = False                    # True = 整轮静默丢弃（如定时任务节流）
    message: "MessageIn | None" = None      # 非 None 时替换原始消息（内容改写）


@dataclass
class IterationDecision:
    """② before_iteration 的决策。"""
    proceed: bool = True
    final_text: str = ""                    # proceed=False 时以此文本结束本轮
    extra_messages: list[dict] = field(default_factory=list)   # 追加到 payload.messages
    exclude_tools: list[str] = field(default_factory=list)     # 本轮临时禁用的工具名


@dataclass
class LLMResponseDecision:
    """③ after_llm_response 的决策。"""
    content: str | None = None              # 非 None 时替换本轮 assistant 文本
    tool_calls: list[ToolCall] | None = None  # 非 None 时替换 tool_calls（慎用，
                                              # 错误替换会破坏 tool_call_id 配对）


@dataclass
class ToolCallDecision:
    """④ before_tool_call 的决策。"""
    proceed: bool = True
    reason: str = ""                        # 否决时作为工具错误返回给 LLM
    result: dict | None = None              # 替换工具结果（mock/缓存短路）


@dataclass
class ToolResultDecision:
    """⑤ before_tool_result 的决策。"""
    content: str | None = None              # 非 None 时替换工具结果 JSON 文本


@dataclass
class MessageOutDecision:
    """⑥ before_msg_out 的决策。"""
    content: str | None = None              # 非 None 时替换回复文本
    suppress: bool = False                  # True 时不持久化、不入队


# ---------------------------------------------------------------------------
# Hooks 基类（默认 no-op）
# ---------------------------------------------------------------------------

class Hooks:
    """默认 no-op；插件可子类化本类，或向 HookChain 注册函数。"""

    async def before_turn(
            self, msg: MessageIn, ctx: HookContext,
    ) -> TurnDecision | None:
        return None

    async def before_iteration(
            self, payload: LLMPayload, ctx: HookContext,
    ) -> IterationDecision | None:
        return None

    async def after_llm_response(
            self, content: str, tool_calls: list[ToolCall], ctx: HookContext,
    ) -> LLMResponseDecision | None:
        return None

    async def before_tool_call(
            self, tool_name: str, kwargs: dict, ctx: HookContext,
    ) -> ToolCallDecision | None:
        return None

    async def before_tool_result(
            self, tool_name: str, kwargs: dict, result: str, ctx: HookContext,
    ) -> ToolResultDecision | None:
        return None

    async def before_msg_out(
            self, msg: MessageOut, ctx: HookContext,
    ) -> MessageOutDecision | None:
        return None


HOOK_NAMES = (
    "before_turn",
    "before_iteration",
    "after_llm_response",
    "before_tool_call",
    "before_tool_result",
    "before_msg_out",
)


# ---------------------------------------------------------------------------
# HookChain：多插件分发
# ---------------------------------------------------------------------------

class HookChain(Hooks):
    """按注册顺序分发到各插件 handler。

    链语义按点位类型固化（对插件透明）：
    - 否决型（before_turn / before_iteration / before_tool_call）：
      首个否决/中止/替换决策胜出，后续 handler 不再调用；
    - 管道型（after_llm_response / before_tool_result / before_msg_out）：
      每个 handler 的替换输出作为下一个 handler 的输入，最终生效；
    - 任何 handler 异常：记日志、视为返回 None，链继续。
    """

    def __init__(self):
        self._handlers: dict[str, list[Callable]] = {n: [] for n in HOOK_NAMES}

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    def on(self, hook_name: str, fn: Callable) -> Callable:
        """注册 handler（签名与 Hooks 同名方法一致）。可作装饰器使用。"""
        if hook_name not in self._handlers:
            raise ValueError(
                f"unknown hook '{hook_name}', expected one of {HOOK_NAMES}")
        self._handlers[hook_name].append(fn)
        return fn

    def register(self, hooks: Hooks) -> None:
        """注册一个 Hooks 子类实例：只接管它覆写过的方法。"""
        for name in HOOK_NAMES:
            if getattr(type(hooks), name) is not getattr(Hooks, name):
                self.on(name, getattr(hooks, name))

    def on_before_turn(self, fn: Callable) -> Callable:
        return self.on("before_turn", fn)

    def on_before_iteration(self, fn: Callable) -> Callable:
        return self.on("before_iteration", fn)

    def on_after_llm_response(self, fn: Callable) -> Callable:
        return self.on("after_llm_response", fn)

    def on_before_tool_call(self, fn: Callable) -> Callable:
        return self.on("before_tool_call", fn)

    def on_before_tool_result(self, fn: Callable) -> Callable:
        return self.on("before_tool_result", fn)

    def on_before_msg_out(self, fn: Callable) -> Callable:
        return self.on("before_msg_out", fn)

    # ------------------------------------------------------------------
    # 分发
    # ------------------------------------------------------------------

    @staticmethod
    async def _safe(fn: Callable, *args) -> Any:
        """单 handler 异常隔离：记日志并视为不干预（fail-open）。"""
        try:
            return await fn(*args)
        except Exception:  # noqa: BLE001 - hook 绝不能拖垮 agent loop
            logger.exception(
                "hook handler failed: %s", getattr(fn, "__qualname__", fn))
            return None

    async def before_turn(
            self, msg: MessageIn, ctx: HookContext,
    ) -> TurnDecision | None:
        replaced: MessageIn | None = None
        for fn in self._handlers["before_turn"]:
            d = await self._safe(fn, msg, ctx)
            if d is None:
                continue
            if not d.proceed:                       # 首个否决胜出，短路
                return d
            if d.message is not None:               # 改写管道串联
                msg = d.message
                replaced = msg
        if replaced is not None:
            return TurnDecision(message=replaced)
        return None

    async def before_iteration(
            self, payload: LLMPayload, ctx: HookContext,
    ) -> IterationDecision | None:
        extra: list[dict] = []
        exclude: list[str] = []
        for fn in self._handlers["before_iteration"]:
            d = await self._safe(fn, payload, ctx)
            if d is None:
                continue
            if not d.proceed:                       # 首个中止胜出，短路
                return d
            extra.extend(d.extra_messages)
            exclude.extend(d.exclude_tools)
        if extra or exclude:
            return IterationDecision(extra_messages=extra, exclude_tools=exclude)
        return None

    async def after_llm_response(
            self, content: str, tool_calls: list[ToolCall], ctx: HookContext,
    ) -> LLMResponseDecision | None:
        new_content: str | None = None
        new_calls: list[ToolCall] | None = None
        for fn in self._handlers["after_llm_response"]:
            d = await self._safe(fn, content, tool_calls, ctx)
            if d is None:
                continue
            if d.content is not None:
                content = d.content                 # 管道：输出作为下一输入
                new_content = content
            if d.tool_calls is not None:
                tool_calls = d.tool_calls
                new_calls = tool_calls
        if new_content is not None or new_calls is not None:
            return LLMResponseDecision(content=new_content, tool_calls=new_calls)
        return None

    async def before_tool_call(
            self, tool_name: str, kwargs: dict, ctx: HookContext,
    ) -> ToolCallDecision | None:
        for fn in self._handlers["before_tool_call"]:
            d = await self._safe(fn, tool_name, kwargs, ctx)
            if d is None:
                continue
            if not d.proceed or d.result is not None:
                return d                            # 首个否决/替换胜出，短路
        return None

    async def before_tool_result(
            self, tool_name: str, kwargs: dict, result: str, ctx: HookContext,
    ) -> ToolResultDecision | None:
        modified: str | None = None
        for fn in self._handlers["before_tool_result"]:
            d = await self._safe(fn, tool_name, kwargs, result, ctx)
            if d is not None and d.content is not None:
                result = d.content                  # 管道串联
                modified = result
        if modified is not None:
            return ToolResultDecision(content=modified)
        return None

    async def before_msg_out(
            self, msg: MessageOut, ctx: HookContext,
    ) -> MessageOutDecision | None:
        modified = False
        for fn in self._handlers["before_msg_out"]:
            d = await self._safe(fn, msg, ctx)
            if d is None:
                continue
            if d.content is not None:
                msg.body.content = d.content  # 仍就地改，保证后续 handler 看到管道结果
                modified = True
            if d.suppress:
                return MessageOutDecision(suppress=True)
        if modified:
            return MessageOutDecision(content=msg.body.content)
        return None