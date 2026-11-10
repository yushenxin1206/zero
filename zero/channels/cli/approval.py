"""
In-place approval UI for the CLI channel.

The protocol is generic: any component may publish an ``ApprovalRequest`` as
a stream event, and this class renders it, reads a single-key answer and
fills the answer back through ``ApprovalBroker``. It knows nothing about
which component asked or why.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time

from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from pydantic import ValidationError

from zero.bus.queue import ApprovalBroker
from zero.models import APPROVAL_TIMEOUT_SECONDS, ApprovalRequest, StreamEvent

logger = logging.getLogger(__name__)


class CliApproval:
    """Renders approval panels and reads the answer on a worker thread."""

    def __init__(self, console: Console, approvals: ApprovalBroker,
                 *, use_ptk: bool | None = None):
        self.console = console
        self.approvals = approvals
        self._use_ptk = (
            (sys.stdin.isatty() and sys.stdout.isatty())
            if use_ptk is None else use_ptk)
        # 进行中审批的 abort 信号（worker 线程轮询）；Ctrl+C 终止渲染时置位，
        # 让倒计时线程尽早退出，避免孤儿线程继续打印并吞掉后续输入按键
        self._abort: threading.Event | None = None

    def abort_pending(self) -> None:
        """Stop an in-flight approval countdown (called on SIGINT)."""
        if self._abort is not None:
            self._abort.set()

    async def handle(self, event: StreamEvent, live: Live) -> None:
        """处理通用审批请求：暂停 Live，阻塞式提问，回填答案。

        只按 ApprovalRequest 协议（models.py）渲染与作答，不认识审批的
        业务来源；答案经 bus.approvals 进程内 Future 回填。请求格式异常
        时记录日志并忽略——发起端会超时 fail-closed。
        """
        try:
            req = ApprovalRequest.model_validate_json(event.content)
        except ValidationError as exc:
            # 只记长度与出错字段：event.content 是审批 JSON，含待审批命令，
            # 长期留存正文没必要；发起端会超时 fail-closed
            loc = exc.errors()[0].get("loc") if exc.errors() else ()
            logger.warning(
                "malformed approval event dropped: len=%d loc=%s",
                len(event.content), loc)
            return
        live.stop()
        abort = threading.Event()
        self._abort = abort
        try:
            # input() 是阻塞调用，放工作线程，事件循环保持可用
            answer = await asyncio.to_thread(self._ask_sync, req, abort)
        finally:
            # 关键：abort 置位必须在引用被清掉之前。若等到外层渲染循环
            # 的 except（它晚于本 finally 执行），self._abort 已被清成
            # None，线程收不到信号，倒计时会继续孤儿运行到超时。这里
            # 无论正常退出还是被取消，都通知工作线程收工（正常退出时线程
            # 本就已结束，置位是空操作）。
            abort.set()
            self._abort = None
            # stop() 已把 overflow 改成 "visible"，恢复裁剪再重启渲染
            live.vertical_overflow = "ellipsis"
            live.start()
        if not self.approvals.resolve(req.req_id, answer):
            logger.warning("approval %s resolve failed (expired?)", req.req_id)

    def _ask_sync(self, req: ApprovalRequest,
                  abort: threading.Event | None = None) -> str:
        """打印审批面板并读取单键答案。在 worker 线程中运行。

        倒计时与后端共用同一个 deadline（发起方写在事件 content 里的 unix
        时间戳），到时自动退出并打印超时拦截面板——不再无限悬挂在
        approval › 提示符上，渲染循环也不会被 input() 卡住导致后续
        流式输出积压。abort 置位（用户 Ctrl+C 终止渲染）时立即退出，
        避免孤儿线程继续倒计时并吞掉下一次输入的按键。
        """
        body = f"[bold]{escape(req.summary)}[/bold]"
        if req.detail:
            body += f"\n[dim]{escape(req.detail)}[/dim]"
        self.console.print(self._panel(body, title=req.title))
        opts = " · ".join(f"{o.key}={o.label}" for o in req.options)
        self.console.print(
            f"[bold yellow]允许执行？[/bold yellow] [dim]{opts}[/dim]")
        # 正常路径以请求携带的 deadline 为准；没带才用共享默认值兜底
        deadline = req.deadline or (time.time() + APPROVAL_TIMEOUT_SECONDS)
        keys = "".join(o.key for o in req.options)
        answer = self._read_key_until(deadline, keys, abort)
        if answer:
            return answer
        hint = escape(req.timeout_hint) if req.timeout_hint else ""
        self.console.print(self._panel(
            f"[bold]{escape(req.summary)}[/bold]\n[dim]{hint}[/dim]",
            title="⏰ 审批超时 · 已按拒绝处理", border_style="red"))
        return ""

    def _panel(self, body: str, **kwargs) -> Panel:
        kwargs.setdefault("width", max(20, self.console.width - 1))
        kwargs.setdefault("title_align", "left")
        kwargs.setdefault("border_style", "yellow")
        return Panel(body, **kwargs)

    def _read_key_until(self, deadline: float, keys: str,
                        abort: threading.Event | None = None) -> str:
        """在 deadline（unix 时间戳）前读取单键答案，到时返回 ""。

        在 worker 线程中运行。答案本来就是单字符，Windows 终端下用
        msvcrt 轮询按键替代阻塞式 input()，从而可以带 deadline；
        非 Windows / 非终端环境回退到原 input() 行为（这些环境通常
        无人值守，后端会超时 fail-closed）。
        """
        if sys.platform == "win32" and self._use_ptk:
            return self._read_key_win32(deadline, keys, abort)
        while True:
            answer = input("approval › ").strip().lower()
            if answer in keys:
                return answer
            self.console.print(
                f"[yellow]请输入 {' / '.join(keys)}[/yellow]")

    def _read_key_win32(self, deadline: float, keys: str,
                        abort: threading.Event | None = None) -> str:
        """msvcrt 单键读取 + 同行倒计时刷新。在 worker 线程中运行。"""
        import msvcrt
        # 丢弃提问前用户预敲的残留按键，避免被误当作答案
        while msvcrt.kbhit():
            msvcrt.getwch()
        last_shown = -1
        while True:
            if abort is not None and abort.is_set():
                # 渲染被 Ctrl+C 终止：立即退出，不再倒计时、不再吞按键
                self._clear_countdown_line()
                return ""
            remain = deadline - time.time()
            if remain <= 0:
                self._clear_countdown_line()
                return ""
            if msvcrt.kbhit():
                ch = msvcrt.getwch().lower()
                if ch in ("\x00", "\xe0"):
                    # 功能键（方向键等）的前缀码，吞掉后续扫描码
                    if msvcrt.kbhit():
                        msvcrt.getwch()
                    continue
                if ch in keys:
                    self._clear_countdown_line()
                    self.console.print(f"approval › [bold]{ch}[/bold]")
                    return ch
                # 其他按键忽略，倒计时继续
            shown = int(remain)
            if shown != last_shown:
                last_shown = shown
                sys.stdout.write(f"\rapproval › ({shown}s 后超时自动拒绝) ")
                sys.stdout.flush()
            time.sleep(0.1)

    @staticmethod
    def _clear_countdown_line():
        sys.stdout.write("\r" + " " * 40 + "\r")
        sys.stdout.flush()
