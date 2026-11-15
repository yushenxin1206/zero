"""
权限守卫：把文件类工具与 shell 的能力申请裁定成档位并分发审批。

判定看**资源**，不看命令文本。三档：
    P  包络内（默认）→ 放行（shell 由内核沙箱执行）
    G  可授予       → 审批面板
    D  不可授予     → 结构化拒绝，**永不进面板**

审批状态只有两处——会话级扩权表（``PermissionState`` 内存表）与
``~/.zero/profile.json``（用户资产）。没有 TTL、没有轮次表：要长期放行就落
profile.json，其余随会话消失。

无人值守渠道（scheduler / subagent / 非交互渠道）一律 fail-closed：
只放行 P 档，G 档拒绝并引导用户预置 ``zero-permission add-root``。
"""
import asyncio
import logging
import shlex
import time
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from zero.bus.queue import MessageBus
from zero.core.cancellation import TurnCancelled
from zero.core.hooks import HookContext, ToolCallDecision
from zero.core.turn_changes import register_granted_roots
from zero.models import (
    APPROVAL_TIMEOUT_SECONDS, CHANNEL_WEB, EVENT_APPROVAL, ApprovalOption,
    ApprovalRecord, ApprovalRequest, ApprovalStatus, StreamEvent)
from zero.permission import envelope
from zero.permission.envelope import PermissionState
from zero.utils.file import require_absolute_path

if TYPE_CHECKING:
    from zero.session.store import SessionStore

logger = logging.getLogger(__name__)

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

_DEGRADED_UNATTENDED = (
    "permission: 本机内核沙箱不可用（探针未通过），且当前渠道无人值守，"
    "命令未执行。这类环境下没有「包络内免审批」，一律 fail-closed"
)
_UNATTENDED_UNSANDBOX = (
    "permission: 该命令命中脱离沙箱白名单，但当前渠道无人值守，命令未执行。"
    "如需长期放行请用户执行 zero-permission add-unsandbox 后重试"
)
_UNATTENDED_UNSANDBOX_WEB = (
    "permission: 该命令命中脱离沙箱白名单，但当前渠道无人值守，命令未执行。"
    "如需长期放行请用户在顶部「权限」页把该命令加入「脱离沙箱命令」后重试"
)
_UNATTENDED_EXPAND = (
    "permission: 该操作申请写入包络外路径（{targets}），但当前渠道无人值守，"
    "操作未执行。请用户执行 zero-permission add-root <path> 后重试"
)
_UNATTENDED_EXPAND_WEB = (
    "permission: 该操作申请写入包络外路径（{targets}），但当前渠道无人值守，"
    "操作未执行。请用户在顶部「权限」页把该目录加入「长期可写根」后重试"
)


def _remedy(ctx: HookContext, *, cli: str, web: str) -> str:
    """按渠道给出可执行的补救路径：Web 指向设置页，其余指向 CLI。"""
    return web if getattr(ctx, "channel", "") == CHANNEL_WEB else cli


async def request(
        bus: MessageBus,
        ctx: HookContext,
        *,
        title: str,
        summary: str,
        detail: str,
        options: tuple[ApprovalOption, ...],
        store: "SessionStore | None" = None,
        timeout_hint: str = "",
) -> str | None:
    """发出审批请求并等待作答。返回选项 key，None = 超时/已终止。

    ``store`` 存在时把最终态写进会话的审批审计文件，供切会话后的历史
    回显使用；写盘失败只记日志，不影响审批结果。``timeout_hint`` 是超时
    时展示给用户的补救路径，由调用方按渠道提供（Web 指向设置页，CLI 指向
    命令行），避免把一种渠道的补救方式教给另一种渠道的用户。
    """
    req_id = uuid4().hex[:12]
    created_at = datetime.now()
    status: ApprovalStatus = "cancelled"
    answer: str | None = None
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    bus.approvals.register(req_id, fut)
    try:
        await bus.stream_queue.enqueue(StreamEvent(
            channel=ctx.channel,
            session_id=ctx.session_id,
            turn_id=ctx.turn_id,
            kind=EVENT_APPROVAL,
            content=ApprovalRequest(
                req_id=req_id,
                title=title,
                summary=summary,
                detail=detail,
                options=list(options),
                timeout_hint=timeout_hint,
                deadline=time.time() + APPROVAL_TIMEOUT_SECONDS,
            ).model_dump_json(),
        ))
        stop_task: asyncio.Task | None = None
        if ctx.stop_event is not None:
            # 审批是最长的 await 点，必须与终止信号竞争；否则用户终止后
            # runner 仍会挂满审批超时。
            stop_task = asyncio.create_task(ctx.stop_event.wait())
        try:
            if stop_task is None:
                answer = await asyncio.wait_for(
                    fut, timeout=APPROVAL_TIMEOUT_SECONDS)
                status = "answered"
                return answer
            done, _pending = await asyncio.wait(
                {fut, stop_task}, timeout=APPROVAL_TIMEOUT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED)
            if ctx.stop_event.is_set():
                raise TurnCancelled()
            if fut in done:
                answer = fut.result()
                status = "answered"
            else:
                status = "timeout"
            return answer
        finally:
            if stop_task is not None:
                stop_task.cancel()
    except (asyncio.TimeoutError, TimeoutError):
        status = "timeout"
        logger.info("permission approval %s 超时", req_id)
        return None
    finally:
        _record_approval(
            store, ctx, req_id=req_id, created_at=created_at,
            title=title, summary=summary, detail=detail,
            options=options, status=status, answer=answer)
        bus.approvals.discard(req_id)


def _record_approval(
        store: "SessionStore | None",
        ctx: HookContext,
        *,
        req_id: str,
        created_at: datetime,
        title: str,
        summary: str,
        detail: str,
        options: tuple[ApprovalOption, ...],
        status: ApprovalStatus,
        answer: str | None,
) -> None:
    """写审批审计记录；任何失败都只记日志，不影响审批结论。"""
    if store is None:
        return
    try:
        store.append_approval(ApprovalRecord(
            req_id=req_id,
            channel=ctx.channel,
            session_id=ctx.session_id,
            turn_id=ctx.turn_id,
            title=title,
            summary=summary,
            detail=detail,
            options=list(options),
            status=status,
            answer=answer or "",
            created_at=created_at,
        ))
    except Exception:  # noqa: BLE001 - 审计失败不能回滚审批
        logger.warning("审批记录写入失败：req=%s", req_id, exc_info=True)


class PermissionGuard:
    """before_tool_call handler（签名与 hook 契约一致）。"""

    def __init__(
            self, msg_bus: MessageBus | None = None, *,
            permission: PermissionState,
    ):
        self.msg_bus = msg_bus
        # 探针缓存、会话扩权表与策略文件路径都在权限状态对象里，由组合根注入
        self.permission = permission

    def _attended(self, ctx: HookContext) -> bool:
        """有人能回答审批吗？非交互渠道与后台来源都算无人值守。

        渠道/来源判定收敛到 ``envelope.attended``：网络放行用的是同一份规则，
        避免"能审批"和"能联网"两套名单各自漂移。
        """
        if self.msg_bus is None:
            return False
        return envelope.attended(ctx)

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
        try:
            # 路径契约只有一份：守卫与工具都调 require_absolute_path，
            # 保证「检查的路径」就是「执行的路径」，不存在相对的拼接岔路
            path = str(require_absolute_path(path))
        except ValueError as exc:
            return ToolCallDecision(
                proceed=False, reason=f"permission: {exc}")
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
        verdict = self.permission.check_readable(path, ctx)
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
            self, targets: list[str], ctx: HookContext, *, title: str,
    ) -> ToolCallDecision | None:
        """返回 None/note 决策 = 放行；返回 decision(proceed=False) = 拒绝。

        批准时用 ``ToolCallDecision(note=...)`` 放行，附注由 runner 并入
        工具结果，让 LLM 知道这次操作经过了审批以及授予的档位。
        """
        refused: list[str] = []
        pending: list[str] = []
        for raw in targets:
            verdict = self.permission.check_writable(raw, ctx)
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
            targets = "、".join(pending)
            return ToolCallDecision(
                proceed=False,
                reason=_remedy(
                    ctx,
                    cli=_UNATTENDED_EXPAND.format(targets=targets),
                    web=_UNATTENDED_EXPAND_WEB.format(targets=targets)))
        answer = await request(
            self.msg_bus, ctx,
            title=title,
            summary="、".join(pending),
            detail="批准后该路径进入可写集合",
            options=_EXPAND,
            store=self.permission.session_store,
            timeout_hint=_remedy(
                ctx,
                cli="已按拒绝处理。需要长期放行请执行 "
                    "zero-permission add-root <path>",
                web="已按拒绝处理。可在顶部「权限」页添加长期可写目录"),
        )
        if answer == "s":
            for p in pending:
                self.permission.grant_write_root(p, ctx, "session")
            # 补基线要扫目录，放到线程里，别阻塞事件循环
            await asyncio.to_thread(register_granted_roots, ctx, pending)
            return ToolCallDecision(
                note=f"用户已批准扩权（本会话）：{'、'.join(pending)}")
        if answer == "a":
            for p in pending:
                self.permission.grant_write_root(p, ctx, "always")
            await asyncio.to_thread(register_granted_roots, ctx, pending)
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

        # 路径参数只收绝对路径：在进入任何档位判断之前统一校验，
        # 避免相对路径被 realpath 按进程 cwd 解释成另一条路径。
        working_dir = kwargs.get("working_dir")
        if working_dir:
            try:
                require_absolute_path(working_dir)
            except ValueError as exc:
                return ToolCallDecision(
                    proceed=False, reason=f"permission: {exc}")
        write_paths: list[str] = []
        for raw in kwargs.get("write_paths") or []:
            try:
                write_paths.append(str(require_absolute_path(raw)))
            except ValueError as exc:
                return ToolCallDecision(
                    proceed=False, reason=f"permission: {exc}")

        # ---- 沙箱不可用：没有「包络内免审批」，每次都要问（fail-closed）----
        if not self.permission.sandbox_available(
                self.permission.sandbox_root(ctx)):
            if not self._attended(ctx):
                return ToolCallDecision(proceed=False, reason=_DEGRADED_UNATTENDED)
            answer = await request(
                self.msg_bus, ctx,
                title="shell 命令审批（内核沙箱不可用）",
                summary=command,
                detail=self.permission.sandbox_unavailable_reason(),
                options=_YN,
                store=self.permission.session_store,
                timeout_hint=_remedy(
                    ctx,
                    cli="已按拒绝处理。运行 zero-permission check 查看"
                        "沙箱不可用的原因",
                    web="已按拒绝处理。可在顶部「权限」页查看沙箱状态"),
            )
            if answer == "y":
                return ToolCallDecision(
                    note="用户已批准（内核沙箱不可用，单次允许）")
            return ToolCallDecision(
                proceed=False, reason="permission: 用户拒绝（内核沙箱不可用）")

        # ---- 脱离沙箱白名单：强制审批，不给「总是」----
        if self.permission.matches_unsandbox(argv):
            if not self._attended(ctx):
                return ToolCallDecision(
                    proceed=False,
                    reason=_remedy(
                        ctx,
                        cli=_UNATTENDED_UNSANDBOX,
                        web=_UNATTENDED_UNSANDBOX_WEB))
            answer = await request(
                self.msg_bus, ctx,
                title="脱离沙箱执行审批",
                summary=command,
                detail="该命令命中脱离沙箱白名单，将在内核沙箱外执行",
                options=_YN,
                store=self.permission.session_store,
                timeout_hint=_remedy(
                    ctx,
                    cli="已按拒绝处理。需要长期放行请执行 "
                        "zero-permission add-unsandbox <命令名>",
                    web="已按拒绝处理。可在顶部「权限」页把该命令加入"
                        "「脱离沙箱命令」"),
            )
            if answer == "y":
                return ToolCallDecision(
                    note="用户已批准脱离沙箱执行（单次允许）")
            return ToolCallDecision(
                proceed=False, reason="permission: 用户拒绝脱离沙箱执行")

        # ---- write_paths 扩权申请：D 档直接拒，G 档走审批 ----
        if write_paths:
            return await self._request_expand(
                write_paths, ctx, title="写入包络外路径审批")

        # ---- P 档：包络内，放行（shell.py 负责包进内核沙箱）----
        return None


def install(
        hooks, msg_bus: MessageBus | None = None, *,
        permission: PermissionState,
) -> PermissionGuard:
    """组合根入口。msg_bus 不传则所有审批一律 fail-closed。"""
    guard = PermissionGuard(msg_bus, permission=permission)
    hooks.on_before_tool_call(guard)
    return guard
