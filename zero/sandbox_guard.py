"""
沙箱守卫：把 shell 工具的能力申请裁定成档位并分发审批。

判定看**资源**，不看命令文本。三档：
    P  包络内（默认）→ 放行，由内核沙箱执行
    G  可授予       → 审批面板
    D  不可授予     → 结构化拒绝，**永不进面板**

审批状态只有两处——会话级扩权表（sandbox）与 ``~/.zero/profile.json``
（用户资产）。没有 TTL、没有轮次表：要长期放行就落 profile.json，
其余随会话消失。

无人值守渠道（scheduler / subagent / 非 CLI）一律 fail-closed：
只放行 P 档，G 档拒绝并引导用户预置 ``zero-sandbox add-root``。
"""
import asyncio
import logging
import shlex
import time
from uuid import uuid4

from zero import sandbox
from zero.bus.queue import MessageBus
from zero.core.cancellation import TurnCancelled
from zero.core.hooks import HookContext, ToolCallDecision
from zero.models import ApprovalOption, ApprovalRequest, StreamEvent

logger = logging.getLogger(__name__)

# 等待用户作答的最长时间；超时按拒绝处理
TIMEOUT = 180

# 面板选项 key 必须是单字符（CLI 按 key 逐键作答）。
# 扩权是长期能力，不给「本次」——否则又要回到 TTL/轮次那套状态机。
_YN = (
    ApprovalOption(key="y", label="允许一次"),
    ApprovalOption(key="n", label="拒绝"),
)
_EXPAND = (
    ApprovalOption(key="s", label="本会话允许"),
    ApprovalOption(key="a", label="总是允许"),
    ApprovalOption(key="n", label="拒绝"),
)

_TIMEOUT_HINT = "已按拒绝处理。需要长期放行请执行 zero-sandbox add-root <path>"

_DEGRADED_UNATTENDED = (
    "sandbox: 本机内核沙箱不可用（探针未通过），且当前渠道无人值守，"
    "命令未执行。这类环境下没有「包络内免审批」，一律 fail-closed"
)
_UNATTENDED_UNSANDBOX = (
    "sandbox: 该命令命中脱离沙箱白名单，但当前渠道无人值守，命令未执行。"
    "如需长期放行请用户执行 zero-sandbox add-unsandbox 后重试"
)
_UNATTENDED_EXPAND = (
    "sandbox: 该命令申请写入包络外路径，但当前渠道无人值守，命令未执行。"
    "请用户执行 zero-sandbox add-root <path> 后重试"
)


async def request(
        bus: MessageBus,
        ctx: HookContext,
        *,
        title: str,
        summary: str,
        detail: str,
        options: tuple[ApprovalOption, ...],
) -> str | None:
    """发出审批请求并等待作答。返回选项 key，None = 超时/已终止。"""
    req_id = uuid4().hex[:12]
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    bus.approvals.register(req_id, fut)
    try:
        await bus.stream_queue.enqueue(StreamEvent(
            channel=ctx.channel,
            session_id=ctx.session_id,
            turn_id=ctx.turn_id,
            kind="approval",
            content=ApprovalRequest(
                req_id=req_id,
                title=title,
                summary=summary,
                detail=detail,
                options=list(options),
                timeout_hint=_TIMEOUT_HINT,
                deadline=time.time() + TIMEOUT,
            ).model_dump_json(),
        ))
        stop_task: asyncio.Task | None = None
        if ctx.stop_event is not None:
            # 审批是最长的 await 点，必须与终止信号竞争；否则用户终止后
            # runner 仍会挂满 TIMEOUT。
            stop_task = asyncio.create_task(ctx.stop_event.wait())
        try:
            if stop_task is None:
                return await asyncio.wait_for(fut, timeout=TIMEOUT)
            done, _pending = await asyncio.wait(
                {fut, stop_task}, timeout=TIMEOUT,
                return_when=asyncio.FIRST_COMPLETED)
            if ctx.stop_event.is_set():
                raise TurnCancelled()
            return fut.result() if fut in done else None
        finally:
            if stop_task is not None:
                stop_task.cancel()
    except (asyncio.TimeoutError, TimeoutError):
        logger.info("sandbox approval %s 超时", req_id)
        return None
    finally:
        bus.approvals.discard(req_id)


class SandboxGuard:
    """before_tool_call handler（签名与 hook 契约一致）。"""

    def __init__(self, msg_bus: MessageBus | None = None):
        self.msg_bus = msg_bus

    def _attended(self, ctx: HookContext) -> bool:
        """有人能回答审批吗？非 CLI 渠道与后台来源都算无人值守。"""
        if self.msg_bus is None or ctx.channel != "cli":
            return False
        origin = ctx.tool_context.origin if ctx.tool_context else None
        if origin is not None and origin.kind in ("scheduler", "subagent"):
            return False
        return True

    async def __call__(
            self, tool_name: str, kwargs: dict, ctx: HookContext,
    ) -> ToolCallDecision | None:
        if tool_name != "shell":
            return None

        command = kwargs.get("command", "")
        argv = shlex.split(command) if command else []

        # ---- 沙箱不可用：没有「包络内免审批」，每次都要问（fail-closed）----
        if not sandbox.available():
            if not self._attended(ctx):
                return ToolCallDecision(proceed=False, reason=_DEGRADED_UNATTENDED)
            answer = await request(
                self.msg_bus, ctx,
                title="shell 命令审批（内核沙箱不可用）",
                summary=command,
                detail=sandbox.unavailable_reason(),
                options=_YN,
            )
            if answer == "y":
                return None
            return ToolCallDecision(
                proceed=False, reason="sandbox: 用户拒绝（内核沙箱不可用）")

        # ---- 脱离沙箱白名单：强制审批，不给「总是」----
        if sandbox.matches_unsandbox(argv):
            if not self._attended(ctx):
                return ToolCallDecision(
                    proceed=False, reason=_UNATTENDED_UNSANDBOX)
            answer = await request(
                self.msg_bus, ctx,
                title="脱离沙箱执行审批",
                summary=command,
                detail="该命令命中脱离沙箱白名单，将在内核沙箱外执行",
                options=_YN,
            )
            if answer == "y":
                return None
            return ToolCallDecision(
                proceed=False, reason="sandbox: 用户拒绝脱离沙箱执行")

        # ---- write_paths 扩权申请：D 档直接拒，G 档走审批 ----
        write_paths = kwargs.get("write_paths") or []
        if write_paths:
            refused: list[str] = []
            pending: list[str] = []
            for raw in write_paths:
                verdict = sandbox.check_writable(raw, ctx)
                if verdict.ok:
                    continue                     # 已在包络内，无需申请
                if verdict.tier == sandbox.D:
                    refused.append(verdict.reason)
                else:
                    pending.append(str(raw))
            if refused:
                return ToolCallDecision(
                    proceed=False,
                    reason="sandbox: 拒绝（不可授予）。" + "；".join(refused))
            if pending:
                if not self._attended(ctx):
                    return ToolCallDecision(
                        proceed=False, reason=_UNATTENDED_EXPAND)
                answer = await request(
                    self.msg_bus, ctx,
                    title="写入包络外路径审批",
                    summary="、".join(pending),
                    detail="批准后该路径进入可写集合",
                    options=_EXPAND,
                )
                if answer == "s":
                    for p in pending:
                        sandbox.grant_write_root(p, ctx, "session")
                elif answer == "a":
                    for p in pending:
                        sandbox.grant_write_root(p, ctx, "always")
                else:
                    return ToolCallDecision(
                        proceed=False,
                        reason="sandbox: 用户拒绝扩权，命令未执行。"
                               "改用包络内路径重试")

        # ---- P 档：包络内，放行（shell.py 负责包进内核沙箱）----
        return None


def install(hooks, msg_bus: MessageBus | None = None) -> SandboxGuard:
    """组合根入口。msg_bus 不传则所有审批一律 fail-closed。"""
    guard = SandboxGuard(msg_bus)
    hooks.on_before_tool_call(guard)
    return guard
