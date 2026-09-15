"""
shell_guard 的 before_tool_call handler：三态判定 + 审批分发。

- 非 shell 工具：直接放行（None）；
- allow / deny：按规则立即放行/否决；
- ask：仅 CLI 交互 turn 支持就地审批；飞书、定时任务（scheduler）、
  无总线环境一律 fail-closed，并引导用 zero-guard 预置规则。
"""
import logging

from bus.queue import MessageBus
from core.hooks import HookContext, ToolCallDecision
from plugins.shell_guard import approval, policy, store

logger = logging.getLogger(__name__)

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

    async def __call__(
            self, tool_name: str, kwargs: dict, ctx: HookContext,
    ) -> ToolCallDecision | None:
        if tool_name != "shell":
            return None

        command = kwargs.get("command", "")
        allow_rules, deny_rules = store.rules()
        verdict = policy.evaluate(command, allow_rules, deny_rules)

        if verdict.tier == "allow":
            return None
        if verdict.tier == "deny":
            return ToolCallDecision(
                proceed=False,
                reason=f"shell_guard: blocked ({verdict.reason})",
            )

        # ---- ask ----
        if ctx.metadata.get("source") == "scheduler":
            return ToolCallDecision(proceed=False, reason=_SCHEDULER_REASON)
        if ctx.channel != "cli" or self.msg_bus is None:
            return ToolCallDecision(proceed=False, reason=_UNATTENDED_REASON)

        answer = await approval.request(
            self.msg_bus,
            channel=ctx.channel,
            session_id=ctx.session_id,
            turn_id=ctx.metadata.get("turn_id", ""),
            metadata=ctx.metadata,
            command=command,
            reason=verdict.reason,
        )

        if answer == "y":
            return None
        if answer == "a":
            store.add("allow", policy.normalize(command))
            return None
        if answer == "w":
            store.add("deny", policy.normalize(command))
            return ToolCallDecision(
                proceed=False, reason="shell_guard: denied by user (always)")
        if answer == "n":
            return ToolCallDecision(
                proceed=False, reason="shell_guard: denied by user")
        # None：审批超时
        return ToolCallDecision(
            proceed=False, reason="shell_guard: 审批超时，已拒绝")
