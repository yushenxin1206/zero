"""
权限守卫：把文件类工具与 shell 的能力申请裁定成档位并分发审批。

判定看**资源**，不看命令文本。三档：
    P  包络内（默认）→ 放行（shell 由内核沙箱执行）
    G  可授予       → 审批面板
    D  不可授予     → 结构化拒绝，**永不进面板**

审批状态只有两处——会话级扩权表（``envelope``）与 ``~/.zero/profile.json``
（用户资产）。没有 TTL、没有轮次表：要长期放行就落 profile.json，
其余随会话消失。

无人值守渠道（scheduler / subagent / 非 CLI）一律 fail-closed：
只放行 P 档，G 档拒绝并引导用户预置 ``zero-permission add-root``。
"""
import asyncio
import logging
import shlex
import time
from uuid import uuid4

from zero.bus.queue import MessageBus
from zero.core.cancellation import TurnCancelled
from zero.core.hooks import HookContext, ToolCallDecision
from zero.models import ApprovalOption, ApprovalRequest, StreamEvent
from zero.permission import envelope

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

# 守卫处理哪些工具。新增任何会碰文件/命令的工具时都必须登记这里——
# tests/test_permission.py::test_guard_covers_all_file_tools 会因此变红。
HANDLED_TOOLS = ("shell", "read", "write", "edit")

_TIMEOUT_HINT = "已按拒绝处理。需要长期放行请执行 zero-permission add-root <path>"

_DEGRADED_UNATTENDED = (
    "permission: 本机内核沙箱不可用（探针未通过），且当前渠道无人值守，"
    "命令未执行。这类环境下没有「包络内免审批」，一律 fail-closed"
)
_UNATTENDED_UNSANDBOX = (
    "permission: 该命令命中脱离沙箱白名单，但当前渠道无人值守，命令未执行。"
    "如需长期放行请用户执行 zero-permission add-unsandbox 后重试"
)
_UNATTENDED_EXPAND = (
    "permission: 该操作申请写入包络外路径（{paths}），但当前渠道无人值守，"
    "操作未执行。请用户执行 zero-permission add-root <path> 后重试"
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
        logger.info("permission approval %s 超时", req_id)
        return None
    finally:
        bus.approvals.discard(req_id)


class PermissionGuard:
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

    # ------------------------------------------------------------------
    # 分发
    # ------------------------------------------------------------------

    async def __call__(
            self, tool_name: str, kwargs: dict, ctx: HookContext,
    ) -> ToolCallDecision | None:
        """fail-closed 包装。

        hook 契约本身是 fail-open（handler 异常只记日志、视为不干预），但权限
        守卫不能接受那个语义——守卫自身的 bug 若被吞成"放行"，就等于边界不存在。
        所以这里自理 fail-closed：任何未预期异常都转成明确的拒绝决策，这也正是
        hooks 文档给 fail-closed 守卫指定的做法（在正常返回路径里返回否决）。

        TurnCancelled 除外：它是终止信号，必须透传。
        """
        try:
            return await self._dispatch(tool_name, kwargs, ctx)
        except TurnCancelled:
            raise
        except Exception as e:  # noqa: BLE001 - 守卫内部异常必须变成拒绝
            logger.exception("permission guard 内部异常，按拒绝处理")
            return ToolCallDecision(
                proceed=False,
                reason=f"permission: 守卫内部异常，已按拒绝处理（fail-closed）：{e}")

    async def _dispatch(
            self, tool_name: str, kwargs: dict, ctx: HookContext,
    ) -> ToolCallDecision | None:
        if tool_name == "shell":
            return await self._shell(kwargs, ctx)
        if tool_name not in HANDLED_TOOLS:
            return None                     # 本守卫只管文件类工具与 shell

        path = kwargs.get("file_path", "")
        if not path:
            return None                     # 参数缺失交给工具自己报错
        if tool_name in ("read", "edit"):
            # edit 既读又写；读方向只有 D 档，没有可授予档
            denied = self._check_read(path, ctx)
            if denied is not None:
                return denied
        if tool_name == "read":
            return None
        return await self._request_expand(
            [path], ctx, title="写入包络外路径审批")

    # ------------------------------------------------------------------
    # 进程内工具：读方向只有 D 档——读是「全开减不可授予」，没有可授予档
    # ------------------------------------------------------------------

    def _check_read(self, path: str, ctx: HookContext) -> ToolCallDecision | None:
        verdict = envelope.check_readable(path, ctx)
        if verdict.ok:
            return None
        return ToolCallDecision(
            proceed=False,
            reason=f"permission: 拒绝（不可授予）。{verdict.reason}"
                   f"（needed: {verdict.needed}）")

    # ------------------------------------------------------------------
    # 写方向扩权：D 档直接拒（不进面板）/ G 档审批 / 授权落表
    # ------------------------------------------------------------------

    async def _request_expand(
            self, paths: list[str], ctx: HookContext, *, title: str,
    ) -> ToolCallDecision | None:
        """返回 None/note 决策 = 放行；返回 decision(proceed=False) = 拒绝。

        批准时用 ``ToolCallDecision(note=...)`` 放行，附注由 runner 并入
        工具结果，让 LLM 知道这次操作经过了审批以及授予的档位。
        """
        refused: list[str] = []
        pending: list[str] = []
        for raw in paths:
            verdict = envelope.check_writable(raw, ctx)
            if verdict.ok:
                continue                          # 已在包络内，无需申请
            if verdict.tier == envelope.D:
                refused.append(verdict.reason)
            else:
                pending.append(str(raw))
        if refused:
            return ToolCallDecision(
                proceed=False,
                reason="permission: 拒绝（不可授予）。" + "；".join(refused))
        if not pending:
            return None
        if not self._attended(ctx):
            return ToolCallDecision(
                proceed=False,
                reason=_UNATTENDED_EXPAND.format(paths="、".join(pending)))
        answer = await request(
            self.msg_bus, ctx,
            title=title,
            summary="、".join(pending),
            detail="批准后该路径进入可写集合",
            options=_EXPAND,
        )
        if answer == "s":
            for p in pending:
                envelope.grant_write_root(p, ctx, "session")
            return ToolCallDecision(
                note=f"用户已批准扩权（本会话）：{'、'.join(pending)}")
        if answer == "a":
            for p in pending:
                envelope.grant_write_root(p, ctx, "always")
            return ToolCallDecision(
                note=f"用户已批准扩权（总是）：{'、'.join(pending)}")
        return ToolCallDecision(
            proceed=False,
            reason="permission: 用户拒绝扩权，操作未执行。"
                   "改用包络内路径重试")

    # ------------------------------------------------------------------
    # shell：内核沙箱平面
    # ------------------------------------------------------------------

    async def _shell(self, kwargs: dict, ctx: HookContext) -> ToolCallDecision | None:
        command = kwargs.get("command", "")
        argv = shlex.split(command) if command else []

        # ---- 沙箱不可用：没有「包络内免审批」，每次都要问（fail-closed）----
        if not envelope.available():
            if not self._attended(ctx):
                return ToolCallDecision(proceed=False, reason=_DEGRADED_UNATTENDED)
            answer = await request(
                self.msg_bus, ctx,
                title="shell 命令审批（内核沙箱不可用）",
                summary=command,
                detail=envelope.unavailable_reason(),
                options=_YN,
            )
            if answer == "y":
                return ToolCallDecision(
                    note="用户已批准（内核沙箱不可用，单次允许）")
            return ToolCallDecision(
                proceed=False, reason="permission: 用户拒绝（内核沙箱不可用）")

        # ---- 脱离沙箱白名单：强制审批，不给「总是」----
        if envelope.matches_unsandbox(argv):
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
                return ToolCallDecision(
                    note="用户已批准脱离沙箱执行（单次允许）")
            return ToolCallDecision(
                proceed=False, reason="permission: 用户拒绝脱离沙箱执行")

        # ---- write_paths 扩权申请：D 档直接拒，G 档走审批 ----
        write_paths = kwargs.get("write_paths") or []
        if write_paths:
            return await self._request_expand(
                list(write_paths), ctx, title="写入包络外路径审批")

        # ---- P 档：包络内，放行（shell.py 负责包进内核沙箱）----
        return None


def install(hooks, msg_bus: MessageBus | None = None) -> PermissionGuard:
    """组合根入口。msg_bus 不传则所有审批一律 fail-closed。"""
    guard = PermissionGuard(msg_bus)
    hooks.on_before_tool_call(guard)
    return guard
