"""
shell_guard 的 before_tool_call handler：三态判定 + 审批分发。

- 非 shell 工具：直接放行（None）；
- allow / deny：按规则立即放行/否决；
- ask：仅 CLI 交互 turn 支持就地审批；飞书、定时任务（scheduler）、
  无总线环境一律 fail-closed，并引导用 zero-guard 预置规则。
"""
import logging
import time

from zero.bus.queue import MessageBus
from zero.core.hooks import HookContext, ToolCallDecision
from zero.plugins.shell_guard import approval, policy, store

logger = logging.getLogger(__name__)

# 放行标记 TTL（秒）：到期后自动失效并清除，防止全局单例长期累积
_SESSION_TTL = 3600   # “本会话放行”最长有效 1 小时
_TURN_TTL = 1800      # “本轮放行”最长有效 30 分钟

# fail-closed 时给 LLM 的引导文案
_UNATTENDED_REASON = (
    "shell_guard: 该命令需要人工审批，但当前渠道无人值守（fail-closed）。"
    "如需放行，请让用户用 zero-guard add allow \"<pattern>\" 预置规则"
)
_SCHEDULER_REASON = (
    "shell_guard: 定时任务无人值守，命令需预置规则"
    "（zero-guard add allow \"<pattern>\"）后才会放行"
)


class ShellGuard:
    """before_tool_call handler（可调用对象，签名与 hook 契约一致）。"""

    def __init__(self, msg_bus: MessageBus | None = None):
        self.msg_bus = msg_bus
        # 会话/本轮放行标记，值为 time.monotonic() 时间戳，带 TTL 惰性过期
        self._session_allow: dict[str, float] = {}
        self._turn_allow: dict[str, float] = {}
        self._current_turn: str | None = None   # 用于轮次切换时清空本轮标记

    @staticmethod
    def _alive(table: dict[str, float], key: str, ttl: float) -> bool:
        """标记是否仍有效；已过期则惰性删除并返回 False。"""
        ts = table.get(key)
        if ts is None:
            return False
        if time.monotonic() - ts > ttl:
            table.pop(key, None)
            return False
        return True

    async def __call__(
            self, tool_name: str, kwargs: dict, ctx: HookContext,
    ) -> ToolCallDecision | None:
        if tool_name != "shell":
            return None

        # 轮次切换：上一轮的“本轮放行”标记立即失效，只保留当前轮
        if self._current_turn != ctx.turn_id:
            self._turn_allow.clear()
            self._current_turn = ctx.turn_id

        command = kwargs.get("command", "")
        allow_rules, deny_rules = store.rules()
        verdict = policy.evaluate(command, allow_rules, deny_rules)

        if verdict.tier == "deny":
            return ToolCallDecision(
                proceed=False,
                reason=f"shell_guard: blocked ({verdict.reason})",
            )

        # 放行：命中 allow 规则，或本会话/本轮已放行 → 跳过审批
        # （deny 已在上方优先拦截，放行标记不会绕过 deny 规则）
        if (verdict.tier == "allow"
                or self._alive(self._session_allow, ctx.session_id,
                               _SESSION_TTL)
                or self._alive(self._turn_allow, ctx.turn_id, _TURN_TTL)):
            return None

        # ---- ask ----
        origin = ctx.tool_context.origin if ctx.tool_context else None
        if origin is not None and origin.kind == "scheduler":
            return ToolCallDecision(proceed=False, reason=_SCHEDULER_REASON)
        if ctx.channel != "cli" or self.msg_bus is None:
            return ToolCallDecision(proceed=False, reason=_UNATTENDED_REASON)

        answer = await approval.request(
            self.msg_bus,
            channel=ctx.channel,
            session_id=ctx.session_id,
            turn_id=ctx.turn_id,
            command=command,
            reason=verdict.reason,
            stop_event=ctx.stop_event,
        )

        if answer == "y":
            return None
        if answer == "a":
            store.add("allow", policy.normalize(command))
            return None
        if answer == "s":
            self._session_allow[ctx.session_id] = time.monotonic()
            return None
        if answer == "t":
            self._turn_allow[ctx.turn_id] = time.monotonic()
            return None
        if answer == "d":
            store.add("deny", policy.normalize(command))
            return ToolCallDecision(
                proceed=False, reason="shell_guard: denied by user (always)")
        if answer == "n":
            return ToolCallDecision(
                proceed=False, reason="shell_guard: denied by user")
        # None：审批超时
        return ToolCallDecision(
            proceed=False, reason="shell_guard: 审批超时，已拒绝")
