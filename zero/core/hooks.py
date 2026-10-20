"""
Hooks in agent loop.

只保留一个点位：``before_tool_call``（kwargs 解析后、tool.run 前）。

这是唯一被真实使用的 hook——权限守卫挂在这一点上，而契约天然匹配它的
两类需求：
    - 否决：``ToolCallDecision.proceed=False``，``reason`` 作为工具错误回给 LLM
    - 长等待：``HookContext.stop_event`` 供审批等待与用户终止竞争

其余五个点位（before_turn / before_iteration / after_llm_response /
before_tool_result / before_msg_out）已删除：全仓库零消费者。需要时再加，
成本是"一个方法 + 一个 Decision 类"。

handler 一律 async；任何 handler 抛异常只记日志、视为 no-op（fail-open），
需要 fail-closed 的守卫应在正常返回路径里返回否决决策。唯一例外：
TurnCancelled 是终止信号（非业务异常），必须透传，不允许被 fail-open 吞掉。
"""
import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from zero.core.cancellation import TurnCancelled
from zero.models import ToolContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

@dataclass
class HookContext:
    """hook 运行时上下文，一次构建、逐 hook 透传。"""
    channel: str
    session_id: str
    turn_id: str = ""                       # 触发本轮的 MessageIn.id
    tool_context: ToolContext | None = None
    stop_event: asyncio.Event | None = None  # 当前 turn 的终止信号；长等待
                                              # hook（如审批）需与之竞争，避免
                                              # 用户终止后仍卡在等待上


# ---------------------------------------------------------------------------
# Decisions —— 返回 None 一律等价于"不干预"
# ---------------------------------------------------------------------------

@dataclass
class ToolCallDecision:
    """before_tool_call 的决策。"""
    proceed: bool = True
    reason: str = ""                        # 否决时作为工具错误返回给 LLM
    result: dict | None = None              # 替换工具结果（mock/缓存短路）


# ---------------------------------------------------------------------------
# Hooks 基类（默认 no-op）
# ---------------------------------------------------------------------------

class Hooks:
    """默认 no-op；插件可子类化本类，或向 HookChain 注册函数。"""

    async def before_tool_call(
            self, tool_name: str, kwargs: dict, ctx: HookContext,
    ) -> ToolCallDecision | None:
        return None


HOOK_NAMES = ("before_tool_call",)


# ---------------------------------------------------------------------------
# HookChain：多 guard 分发
# ---------------------------------------------------------------------------

class HookChain(Hooks):
    """按注册顺序分发到各 handler。

    语义按点位类型固化（对 guard 透明）：首个否决/替换决策胜出，
    后续 handler 不再调用；任何 handler 异常：记日志、视为返回 None，链继续。
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

    def on_before_tool_call(self, fn: Callable) -> Callable:
        return self.on("before_tool_call", fn)

    # ------------------------------------------------------------------
    # 分发
    # ------------------------------------------------------------------

    @staticmethod
    async def _safe(fn: Callable, *args) -> Any:
        """单 handler 异常隔离：记日志并视为不干预（fail-open）。

        TurnCancelled 除外：它是终止信号，不是业务异常，必须原样透传，
        否则用户终止会被吞成"不干预"导致 turn 继续运行。
        """
        try:
            return await fn(*args)
        except TurnCancelled:
            raise
        except Exception:  # noqa: BLE001 - hook 绝不能拖垮 agent loop
            logger.exception(
                "hook handler failed: %s", getattr(fn, "__qualname__", fn))
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
