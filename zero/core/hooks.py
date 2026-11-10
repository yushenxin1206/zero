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
import functools
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Literal

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
    note: str = ""                          # 附注：proceed=True 时并入工具结果，
                                            # 随 role=tool 消息进入上下文


# ---------------------------------------------------------------------------
# Hooks 基类（默认 no-op）
# ---------------------------------------------------------------------------

class Hooks:
    """默认 no-op；插件可子类化本类，或向 HookChain 注册函数。"""

    async def before_tool_call(
            self, tool_name: str, kwargs: dict, ctx: HookContext,
    ) -> ToolCallDecision | None:
        return None


HookName = Literal["before_tool_call"]
HookHandler = Callable[[str, dict, HookContext], Awaitable[ToolCallDecision | None]]

# 单点位的名字清单；HOOK_NAMES 与 Hooks 基类方法的一致性由测试锁定
HOOK_NAMES: tuple[HookName, ...] = ("before_tool_call",)


def _check_handler(hook_name: HookName, fn: HookHandler) -> None:
    """注册期校验 handler 契约：必须是 async、且接受 3 个位置参数。

    签名漂移如果在分发时才暴露，会被 ``_safe`` 的宽 except 吞成静默 no-op
    ——对 fail-open 的护栏来说等于护栏不存在。所以在注册处直接报错。
    """
    # 可调用对象（如 PermissionGuard）的协程性在 __call__ 上；直接对实例
    # 调 iscoroutinefunction 恒为 False，会误杀合法的 async __call__ 守卫
    target = fn if (
        inspect.isfunction(fn) or inspect.ismethod(fn)
        or isinstance(fn, functools.partial)
    ) else getattr(type(fn), "__call__", None)
    if target is None or not inspect.iscoroutinefunction(target):
        raise TypeError(
            f"hook '{hook_name}' handler {fn!r} 必须是 async 函数")
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError) as e:
        raise TypeError(
            f"hook '{hook_name}' handler {fn!r} 签名不可读: {e}") from e
    positional = [
        p for p in params
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    if not any(p.kind is p.VAR_POSITIONAL for p in params) \
            and len(positional) != 3:
        raise TypeError(
            f"hook '{hook_name}' handler {fn!r} 必须接受 3 个位置参数"
            f"（tool_name, kwargs, ctx），实际 {len(positional)} 个")


# ---------------------------------------------------------------------------
# HookChain：多 guard 分发
# ---------------------------------------------------------------------------

class HookChain(Hooks):
    """按注册顺序分发到各 handler。

    语义按点位类型固化（对 guard 透明）：首个否决/替换决策胜出，
    后续 handler 不再调用；任何 handler 异常：记日志、视为返回 None，链继续。
    """

    def __init__(self):
        self._handlers: dict[HookName, list[HookHandler]] = {
            n: [] for n in HOOK_NAMES}

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    def on(self, hook_name: HookName, fn: HookHandler) -> HookHandler:
        """注册 handler（签名与 Hooks 同名方法一致）。可作装饰器使用。"""
        if hook_name not in self._handlers:
            raise ValueError(
                f"unknown hook '{hook_name}', expected one of {HOOK_NAMES}")
        _check_handler(hook_name, fn)
        self._handlers[hook_name].append(fn)
        return fn

    def register(self, hooks: Hooks) -> None:
        """注册一个 Hooks 子类实例：只接管它覆写过的方法。"""
        for name in HOOK_NAMES:
            if getattr(type(hooks), name) is not getattr(Hooks, name):
                self.on(name, getattr(hooks, name))

    def on_before_tool_call(self, fn: HookHandler) -> HookHandler:
        return self.on("before_tool_call", fn)

    def handlers_for(self, hook_name: HookName) -> tuple[HookHandler, ...]:
        """该点位已注册的 handler 快照（按注册顺序，只读）。"""
        return tuple(self._handlers[hook_name])

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
        notes: list[str] = []
        for fn in self.handlers_for("before_tool_call"):
            d = await self._safe(fn, tool_name, kwargs, ctx)
            if d is None:
                continue
            if d.note:
                notes.append(d.note)
            if not d.proceed or d.result is not None:
                # 首个否决/替换胜出，短路；此前累积的附注一并带上
                return replace(d, note="\n".join(notes))
        # 仅附注：不拦截也不替换，放行后由 runner 并入工具结果
        return ToolCallDecision(note="\n".join(notes)) if notes else None
