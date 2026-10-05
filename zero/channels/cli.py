"""
CLI channel module — interactive terminal chat powered by rich.

Features:
  1. Session picker on startup: continue latest / pick a historical
     session / create a new one (channel = "cli").
  2. Real-time streaming rendering of the assistant reply (Live + Markdown),
     with tool-call status indicators.
  3. Slash commands: /new /sessions /title /tasks /bgstream /history /help /quit
  4. Standing outbound consumers: results of background turns (scheduled
     tasks, interrupted turns) are demultiplexed by turn_id and rendered
     asynchronously above the input prompt — they no longer depend on the
     user submitting a message to be seen.

Input runs in a worker thread (blocking input()) while the event loop
keeps consuming stream events, so rendering stays live.
"""
import asyncio
import json
import logging
import sys
import time
from uuid import uuid4

from pydantic import ValidationError
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Prompt
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from zero.bus.queue import MessageBus
from zero.channels.cli_prompt import COMMANDS, CommandStats, build_prompt_session
from zero.config import Config
from zero.core.model_switch import session_model, switch_model
from zero.models import (
    ApprovalRequest, MessageBody, MessageIn, MessageOut, Origin, StreamEvent)
from zero.session.models import SessionMeta
from zero.tools.registry import ToolRegistry
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.formatted_text import ANSI as PtkANSI
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import print_formatted_text
from prompt_toolkit.styles import Style
from zero.scheduler.store import SchedulerStore
from zero.session.store import SESSIONS_ROOT, SessionStore
from zero.utils.notify import toast

logger = logging.getLogger(__name__)

CHANNEL = "cli"
BINDING_PATH = SESSIONS_ROOT / CHANNEL / ".cli_current.json"

# 输入提示符样式（bold cyan），与历史消息中的 "you ›" 一致；
# 只作用于 class:prompt 片段，不影响用户输入的文字
_PROMPT_STYLE = Style.from_dict({"prompt": "bold ansicyan"})

# alias -> canonical command (canonical names defined in cli_prompt.COMMANDS)
_COMMAND_ALIASES = {
    "/n": "/new", "/s": "/sessions", "/h": "/history", "/t": "/title",
    "/ts": "/tasks", "/?": "/help", "/q": "/quit", "/exit": "/quit",
}


class CLIChannel:
    """
    Interactive CLI channel.

    Usage:
        channel = CLIChannel(msg_bus)
        await channel.start()   # runs the menu + chat loop, returns on /quit
    """

    def __init__(self, msg_bus: MessageBus,
                 config: Config | None = None,
                 tool_registry: ToolRegistry | None = None):
        self.msg_bus = msg_bus
        self.config = config
        self.tool_registry = tool_registry
        self.console = Console(highlight=False)
        self._out_queue = msg_bus.subscribe_out(CHANNEL)
        self._stream_queue = msg_bus.subscribe_stream(CHANNEL)
        # current session binding
        self._source_id = ""
        self.session_id = ""
        # interactive prompt with '/' command completion + autosuggestion;
        # prompt_toolkit needs a real console — fall back to plain input()
        # when stdin/stdout are piped (e.g. CI, tests, shell pipes)
        self._stats = CommandStats()
        self._use_ptk = sys.stdin.isatty() and sys.stdout.isatty()
        self._prompt = build_prompt_session(self._stats) if self._use_ptk else None

        # ---- turn demultiplexing state (standing consumers) ----
        # turn currently rendered by _render_turn; set before the MessageIn
        # is enqueued so early events cannot be misrouted to the bg pipeline
        self._active_turn_id = ""
        # events / final MessageOut belonging to the active interactive turn
        self._turn_events: asyncio.Queue[StreamEvent] = asyncio.Queue()
        self._turn_out: asyncio.Queue[MessageOut] = asyncio.Queue()
        # serializes Live rendering and async-result printing so a scheduled
        # result never tears into an active streaming frame
        self._render_lock = asyncio.Lock()
        # background turns (scheduled tasks, interrupted turns) being accumulated
        self._bg_turns: dict[str, dict] = {}
        # turn_ids already finalized (dedup stream-done vs MessageOut, and
        # discard late MessageOut of a normally finished interactive turn)
        self._bg_finalized: set[str] = set()
        # completed background results waiting to be rendered (FIFO worker)
        self._async_results: asyncio.Queue[dict] = asyncio.Queue()
        # results rendered since the last user input (prompt ⏰N indicator)
        self._unread = 0
        # /bgstream toggle: stream background turn deltas live above the prompt
        self._bg_stream_enabled = False
        # True while prompt_async is awaiting input (ptk app running);
        # async prints use the ptk-native above-prompt path only then
        self._prompt_active = False
        self._listener_tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------------
    # Session binding persistence
    # ------------------------------------------------------------------

    def _load_binding(self) -> dict | None:
        if BINDING_PATH.exists():
            try:
                return json.loads(BINDING_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
        return None

    def _save_binding(self):
        BINDING_PATH.parent.mkdir(parents=True, exist_ok=True)
        BINDING_PATH.write_text(json.dumps({
            "source_id": self._source_id,
            "session_id": self.session_id,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    def _bind_session(self, source_id: str, session_id: str):
        self._source_id = source_id
        self.session_id = session_id
        self._save_binding()

    def _new_session(self):
        source_id = f"cli-{uuid4().hex[:8]}"
        session_id = SessionStore.create_session(CHANNEL, source_id)
        self._bind_session(source_id, session_id)

    # ------------------------------------------------------------------
    # Startup menu
    # ------------------------------------------------------------------

    def _print_banner(self):
        title = SessionStore.get_title(CHANNEL, self.session_id)
        title_part = f" · [bold]{escape(title)}[/bold]" if title else ""
        self.console.print(self._panel(
            "[bold]zero[/bold] · cli channel\n"
            f"session [dim]{self.session_id[:8]}[/dim]{title_part} · "
            "commands: /new /sessions /title /tasks /bgstream /history /help /quit",
            border_style="cyan",
        ))

    def _title_cell(self, s: SessionMeta) -> str:
        """Title column: stored title, or a dim fallback derived from the
        first user message for sessions created before titles existed."""
        if s.title:
            return escape(s.title)
        fallback = SessionStore.make_title(
            SessionStore.first_user_message(CHANNEL, s.session_id))
        return f"[dim]{escape(fallback)}[/dim]" if fallback else "[dim]-[/dim]"

    def _session_table(self, sessions: list[SessionMeta]) -> Table:
        table = Table(title=f"历史会话 (channel={CHANNEL})", expand=True)
        table.add_column("#", style="bold", justify="right")
        table.add_column("日期")
        table.add_column("最近更新")
        table.add_column("标题")
        table.add_column("会话 ID", style="dim")
        for i, s in enumerate(sessions[:20], 1):
            marker = " [bold green]← 当前[/]" if s.session_id == self.session_id else ""
            table.add_row(
                str(i),
                s.date,
                s.last_updated_at.strftime("%Y-%m-%d %H:%M:%S"),
                self._title_cell(s),
                s.session_id[:13] + marker,
            )
        return table

    def _pick_session(self) -> bool:
        """Show the session menu. Returns False if the user chose to quit."""
        while True:
            # 空会话（创建后从未对话）不参与排序选择，避免"最近会话"错位
            sessions = [
                s for s in SessionStore.list_sessions(CHANNEL)
                if SessionStore.has_messages(CHANNEL, s.session_id)
            ]
            if sessions:
                self.console.print(self._session_table(sessions))
            else:
                self.console.print("[dim]暂无历史会话[/dim]")

            default = "1" if sessions else "n"
            choice = Prompt.ask(
                "[bold cyan]选择[/bold cyan] "
                "(Enter=继续最近, 序号=历史会话, n=新建, q=退出)",
                default=default,
            ).strip().lower()

            if choice in ("q", "quit", "exit"):
                return False
            if choice == "":
                if sessions:
                    entry = sessions[0]
                    self._bind_session(entry.source_id, entry.session_id)
                else:
                    self._new_session()
                return True
            if choice in ("n", "new"):
                self._new_session()
                return True
            if choice.isdigit() and 1 <= int(choice) <= min(len(sessions), 20):
                entry = sessions[int(choice) - 1]
                self._bind_session(entry.source_id, entry.session_id)
                return True
            self.console.print("[yellow]无效输入，请重试[/yellow]")

    # ------------------------------------------------------------------
    # History display
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Message rendering (shared by history display and live streaming,
    # so past and incoming messages look identical)
    # ------------------------------------------------------------------

    TRUNCATED = "...truncated"

    @staticmethod
    def _clip(text: str, max_len: int) -> str:
        return text if len(text) <= max_len else text[:max_len] + CLIChannel.TRUNCATED

    def _panel(self, renderable, **kwargs) -> Panel:
        """构造宽度 = 终端宽度 - 1 的 Panel。

        末列留余量：一些终端的 overlay 滚动条会遮住最后一列，且部分
        终端对 emoji 的单元格宽度计算与 rich 不一致，右边框贴边时
        容易看起来"没有闭合"。
        """
        kwargs.setdefault("width", max(20, self.console.width - 1))
        return Panel(renderable, **kwargs)

    def _assistant_panel(self, text: str):
        body: object = Markdown(text) if text else Text("(thinking ...)", style="dim")
        return self._panel(
            body, title="zero",
            title_align="left", border_style="green",
        )

    def _render_message_line(self, m, max_len: int = 600):
        # 压缩摘要（load_history 合成的消息）：独立样式，避免被误读为用户发言
        if m.metadata.get("kind") == "compact":
            self.console.print(
                f"[dim magenta]compact ›[/dim magenta] "
                f"{self._clip(m.body.content or '', max_len)}")
            return
        if m.sender == "user":
            self.console.print(
                f"[bold cyan]you ›[/bold cyan] {self._clip(m.body.content or '', max_len)}")
        elif m.sender == "assistant" and m.body.content:
            self.console.print(
                self._assistant_panel(self._clip(m.body.content, max_len)))

    def _print_recent_history(self, limit: int = 6):
        history = SessionStore.load_history(self.session_id, CHANNEL)
        if not history:
            return
        shown = [m for m in history if m.sender in ("user", "assistant")]
        self.console.print(f"[dim]—— 最近 {min(len(shown), limit)} 条历史消息 ——[/dim]")
        for m in shown[-limit:]:
            self._render_message_line(m)

    def _print_full_history(self):
        history = SessionStore.load_history(self.session_id, CHANNEL)
        if not history:
            self.console.print("[dim]当前会话暂无消息[/dim]")
            return
        if sys.platform == "win32":
            # Windows 下 console.pager() 走 pydoc tempfile_pager，用本地代码页
            # (GBK) 写临时文件再过 more.com："›" 不在 GBK 中，会被
            # backslashreplace 成字面量 r"\u203a"。绕过 pager 直接输出，
            # 翻页交给终端 scrollback。
            self._print_history_lines(history)
            return
        with self.console.pager():
            self._print_history_lines(history)

    def _print_history_lines(self, history):
        # 只显示 user / assistant 会话内容，过滤 tool 等内部过程数据
        for m in history:
            if m.sender == "tool":
                continue
            self._render_message_line(m, max_len=100000)

    # ------------------------------------------------------------------
    # Chat loop
    # ------------------------------------------------------------------

    async def start(self):
        """Entry point: pick/restore session, then run the chat loop."""
        binding = self._load_binding()
        if binding:
            self._bind_session(binding["source_id"], binding["session_id"])
            title = SessionStore.get_title(CHANNEL, self.session_id)
            title_part = f"「{title}」" if title else ""
            self.console.print(
                f"[dim]已恢复会话 {self.session_id[:8]}{title_part}"
                "（输入 /sessions 可切换）[/dim]")
            if not self._confirm_or_switch():
                return
        else:
            if not self._pick_session():
                return

        self._print_banner()
        self._print_recent_history()

        # 常驻出站消费者：stream / out 队列各一个，外加异步结果渲染 worker。
        # 定时任务等后台 turn 的结果不再依赖用户发消息才被看到。
        self._listener_tasks = [
            asyncio.create_task(self._stream_listener()),
            asyncio.create_task(self._out_listener()),
            asyncio.create_task(self._async_render_worker()),
        ]
        try:
            await self._chat_loop()
        finally:
            for t in self._listener_tasks:
                t.cancel()

    def _confirm_or_switch(self) -> bool:
        """After restoring a binding, let the user enter the session or re-pick."""
        choice = Prompt.ask(
            "[bold cyan]回车进入会话[/bold cyan]，输入 s 重新选择，q 退出",
            default="",
        ).strip().lower()
        if choice in ("q", "quit"):
            return False
        if choice in ("s", "sessions", "l", "list"):
            return self._pick_session()
        return True

    async def _read_input(self) -> str:
        """Read one line of user input (prompt_toolkit or plain fallback).

        The "you ›" prompt is styled bold cyan to match the history display.
        The patch_stdout() context manager lets async prints (scheduled-task
        results) render above the prompt without disturbing the line being
        typed. raw=True is required: the default raw=False routes through
        Output.write(), which escapes ANSI sequences — rich output would
        show up as literal "?[2m ... ?[0m" garbage. write_raw passes them
        through to Windows10_Output, which renders VT colors natively.
        (prompt_async itself has no patch_stdout kwarg — passing one
        raises TypeError on prompt_toolkit 3.x.)
        """
        if self._prompt is not None:
            # 未读指示符：上一轮输入以来到达的后台结果数
            prompt_text = "you › " if not self._unread else f"you ⏰{self._unread} › "
            self._prompt_active = True
            try:
                with patch_stdout(raw=True):
                    return await self._prompt.prompt_async(
                        [("class:prompt", prompt_text)],
                        style=_PROMPT_STYLE,
                        bottom_toolbar=self._toolbar_line(),
                    )
            finally:
                self._prompt_active = False
        # 非终端回退：输入无 prompt，提交后用 rich 打印高亮行，与历史一致
        text = await asyncio.to_thread(input, "")
        self.console.print(f"[bold cyan]you ›[/bold cyan] {text}")
        return text

    def _toolbar_line(self) -> str:
        """常驻底部状态行：展示本会话当前生效模型（channel 无关查询）。"""
        model = "?"
        if self.config is not None:
            try:
                model = session_model(self.config, CHANNEL, self.session_id)
            except ValueError:
                model = "?"
        return (f" model: {model} "
                f"· session: {self.session_id[:8]}")

    async def _chat_loop(self):
        while True:
            try:
                # Ctrl+C / Ctrl+D 均视为退出
                user_text = await self._read_input()
            except (EOFError, KeyboardInterrupt):
                self.console.print("\n[dim]bye[/dim]")
                return

            text = (user_text or "").strip()
            self._unread = 0  # 用户已回到终端，清零未读指示
            if not text:
                continue
            if text.startswith("/"):
                if not await self._handle_command(text):
                    return
                continue

            msg = MessageIn(
                channel=CHANNEL,
                session_id=self.session_id,
                body=MessageBody(content=text),
                channel_message_id=str(uuid4()),
                channel_sender_id=self._source_id,
                channel_session_id=self._source_id,
            )
            # 先入激活 turn 再入队，保证 listener 不会把本轮最早的事件
            # 误判成后台 turn
            self._active_turn_id = msg.id
            await self.msg_bus.in_queue.enqueue(msg)
            try:
                await self._render_turn(msg.id)
            except asyncio.CancelledError:
                # Ctrl+C during generation: abort rendering; the agent keeps
                # running in the background and the remaining events flow
                # into the background pipeline, so the finished reply is
                # still rendered asynchronously when it completes.
                self.console.print(
                    "\n[yellow]已中断渲染，回答完成后将自动显示[/yellow]")

    def _handle_title(self, arg: str):
        """`/title` shows the current title; `/title <text>` renames."""
        arg = arg.strip()
        if not arg:
            current = SessionStore.get_title(CHANNEL, self.session_id)
            if current:
                self.console.print(f"当前标题: [bold]{escape(current)}[/bold]")
            else:
                self.console.print(
                    "[dim]当前会话暂无标题，用法: /title <新标题>[/dim]")
            return
        if SessionStore.set_title(CHANNEL, self.session_id, arg):
            self.console.print(f"[green]标题已更新:[/green] {escape(arg)}")
        else:
            self.console.print("[yellow]当前会话不在索引中，无法命名[/yellow]")

    def _handle_tasks(self, arg: str):
        """`/tasks` 列出当前会话的定时任务；`/tasks all` 列出 cli 渠道全部。"""
        show_all = arg.strip().lower() == "all"
        tasks = [t for t in SchedulerStore.load_all() if t.channel == CHANNEL]
        if not show_all:
            tasks = [t for t in tasks if t.session_id == self.session_id]
        if not tasks:
            hint = "" if show_all else "（/tasks all 查看全部）"
            self.console.print(f"[dim]暂无定时任务{hint}[/dim]")
            return

        status_style = {
            "pending": "yellow", "running": "green",
            "done": "dim", "failed": "red", "cancelled": "dim",
        }
        table = Table(
            title=f"定时任务（{'全部' if show_all else '当前会话'}）", expand=True)
        table.add_column("状态", justify="center")
        table.add_column("类型")
        table.add_column("下次触发")
        table.add_column("最近执行")
        table.add_column("指令")
        table.add_column("ID", style="dim")

        def _fmt(dt) -> str:
            return dt.strftime("%m-%d %H:%M") if dt else "-"

        for t in sorted(tasks, key=lambda x: (x.trigger_at is None, x.trigger_at or x.created_at)):
            style = status_style.get(t.status, "")
            instruction = t.instruction.removeprefix("[Scheduled Task]")
            table.add_row(
                f"[{style}]{t.status}[/{style}]" if style else t.status,
                t.schedule_type + (f" {t.cron}" if t.cron else ""),
                _fmt(t.trigger_at),
                _fmt(t.last_run_at),
                escape(self._clip(instruction, 40)),
                t.id[:8],
            )
        self.console.print(table)

    def _handle_bgstream(self, arg: str):
        """`/bgstream` 切换定时任务结果的流式输出；`/bgstream on|off` 显式设置。"""
        arg = arg.strip().lower()
        if arg == "on":
            self._bg_stream_enabled = True
        elif arg == "off":
            self._bg_stream_enabled = False
        else:
            self._bg_stream_enabled = not self._bg_stream_enabled
        if self._bg_stream_enabled:
            self.console.print(
                "[green]定时任务流式输出已开启[/green] "
                "[dim]（任务输出将实时打印在输入行上方）[/dim]")
        else:
            self.console.print(
                "[green]定时任务流式输出已关闭[/green] "
                "[dim]（任务完成后一次性展示结果）[/dim]")

    async def _handle_model(self, arg: str) -> None:
        """查看 / 切换本会话模型（候选来自 config.models 注册表）。"""
        config = self.config
        if config is None:
            self.console.print("[yellow]未注入配置，无法切换模型[/yellow]")
            return
        models = config.models
        if not models:
            self.console.print("[dim]models 注册表为空[/dim]")
            return
        current = session_model(config, CHANNEL, self.session_id)
        name = arg.strip()
        if not name:
            keys = sorted(models)
            lines = "\n".join(
                f"  {i + 1}. {k}{' ← 当前' if k == current else ''}"
                for i, k in enumerate(keys))
            self.console.print(
                f"[bold]当前模型[/bold]: {current}\n"
                f"[bold]可用模型[/bold]\n{lines}")
            choice = Prompt.ask("输入编号切换（回车取消）", default="").strip()
            if not choice:
                self.console.print("[dim]已取消[/dim]")
                return
            try:
                name = keys[int(choice) - 1]
            except (ValueError, IndexError):
                self.console.print(f"[yellow]无效编号 {choice}[/yellow]")
                return
        try:
            r = await switch_model(
                config=config, channel=CHANNEL, session_id=self.session_id,
                new_model=name,
                tools=(self.tool_registry.schemas()
                       if self.tool_registry is not None else None),
            )
        except ValueError as e:
            self.console.print(f"[red]{e}[/red]")
            return
        if not r.switched:
            self.console.print(f"[dim]当前已是 {r.new_model}，无需切换[/dim]")
            return
        note = " · [bold green]已按新窗口压缩会话[/]" if r.compacted else ""
        self.console.print(
            f"[green]模型已切换[/green] {r.old_model} → "
            f"[bold]{r.new_model}[/bold]（窗口 {r.window_tokens}）{note}")

    async def _handle_command(self, text: str) -> bool:
        # 需要 await 的分支以外，其余与原同步行为一致（见各分支）
        cmd, _, _arg = text.partition(" ")
        cmd = cmd.lower()
        canonical = _COMMAND_ALIASES.get(cmd, cmd)
        if canonical in {c for c, _ in COMMANDS}:
            self._stats.record(canonical)
        if cmd in ("/quit", "/exit", "/q"):
            self.console.print("[dim]bye[/dim]")
            return False
        if cmd in ("/new", "/n"):
            self._new_session()
            self._print_banner()
            return True
        if cmd in ("/sessions", "/s"):
            if self._pick_session():
                self._print_banner()
                self._print_recent_history()
            return True
        if cmd in ("/title", "/t"):
            self._handle_title(_arg)
            return True
        if cmd in ("/model", "/m"):
            await self._handle_model(_arg)
            return True
        if cmd in ("/tasks", "/ts"):
            self._handle_tasks(_arg)
            return True
        if cmd == "/bgstream":
            self._handle_bgstream(_arg)
            return True
        if cmd in ("/history", "/h"):
            self._print_full_history()
            return True
        if cmd in ("/help", "/?"):
            lines = "\n".join(f"  {c:<12} {desc}" for c, desc in COMMANDS)
            self.console.print(
                f"[bold]可用命令[/bold]\n{lines}\n\n"
                "[dim]输入 / 自动弹出命令补全（按使用频率排序），"
                "Tab 选择，→ 或 Ctrl+E 接受灰色建议[/dim]")
            return True
        self.console.print(f"[yellow]未知命令 {cmd}，输入 /help 查看[/yellow]")
        return True

    # ------------------------------------------------------------------
    # Streaming render
    # ------------------------------------------------------------------

    def _frame(self, acc: str, status: str):
        # 与历史渲染共用 _assistant_panel，保证新消息与历史消息样式一致
        body = self._assistant_panel(acc)
        if status:
            return Group(
                Spinner("dots", text=status, style="green"),
                body,
            )
        return body

    async def _render_turn(self, turn_id: str):
        """Render one interactive turn live, consuming events that the
        standing stream listener routed into this turn's private queue."""
        acc = ""
        status = ""
        usage: dict | None = None
        live = Live(
            self._frame(acc, status),
            console=self.console,
            refresh_per_second=12,
            vertical_overflow="visible",
        )
        done = False
        try:
            # 持证渲染：定时任务结果等异步输出会等本轮 Live 结束后再打印，
            # 避免撕裂流式画面
            async with self._render_lock:
                live.start()
                while not done:
                    event = await self._turn_events.get()
                    if event.turn_id != turn_id:
                        # 防御：listener 已按 active turn 路由，正常不会走到
                        continue
                    if event.kind == "delta":
                        acc += event.content
                    elif event.kind == "tool_call":
                        status = f"调用工具 {self._clip(event.content, 90)}"
                    elif event.kind == "approval":
                        # 通用就地审批：暂停 Live 提问，回答回填后恢复渲染
                        await self._handle_approval(event, live)
                    elif event.kind == "error":
                        live.stop()
                        self.console.print(f"[red]出错:[/red] {event.content}")
                        done = True  # 视为终结，迟到的 MessageOut 不再重复渲染
                    elif event.kind == "done":
                        if event.content and not acc:
                            acc = event.content
                        if u := (event.metadata or {}).get("token_usage"):
                            usage = u
                        done = True
                    live.update(self._frame(acc, status))
        finally:
            live.stop()
            # 正常终结（done/error）的 turn：迟到的 MessageOut 由后台管线丢弃；
            # 被 Ctrl+C 中断的 turn 不标记，剩余事件流入后台管线，
            # 完成后作为「后台回答」自动渲染
            if done:
                self._bg_finalized.add(turn_id)
            if self._active_turn_id == turn_id:
                self._active_turn_id = ""
            if usage:
                self.console.print(
                    f"[dim]tokens: {usage.get('promptTokens', 0)} in / "
                    f"{usage.get('completionTokens', 0)} out / "
                    f"{usage.get('totalTokens', 0)} total[/dim]"
                )
            self.console.print()

        # Drain the matching final MessageOut so queues stay in sync.
        try:
            await asyncio.wait_for(self._turn_out.get(), timeout=2)
        except (asyncio.TimeoutError, TimeoutError):
            pass

    # ------------------------------------------------------------------
    # In-place approval (generic ApprovalRequest protocol)
    # ------------------------------------------------------------------

    async def _handle_approval(self, event: StreamEvent, live: Live):
        """处理通用审批请求：暂停 Live，阻塞式提问，回填答案。

        只按 ApprovalRequest 协议（models.py）渲染与作答，不认识审批的
        业务来源；答案经 bus.approvals 进程内 Future 回填。请求格式异常
        时记录日志并忽略——发起端会超时 fail-closed。
        """
        try:
            req = ApprovalRequest.model_validate_json(event.content)
        except ValidationError:
            logger.warning("malformed approval event dropped: %r",
                           event.content[:200])
            return
        live.stop()
        try:
            # input() 是阻塞调用，放工作线程，事件循环保持可用
            answer = await asyncio.to_thread(self._ask_approval_sync, req)
        finally:
            live.start()
        if not self.msg_bus.approvals.resolve(req.req_id, answer):
            logger.warning("approval %s resolve failed (expired?)", req.req_id)

    def _ask_approval_sync(self, req: ApprovalRequest) -> str:
        """打印审批面板并读取单键答案。在 worker 线程中运行。

        倒计时与后端共用同一个 deadline（发起方写在事件 content 里的 unix
        时间戳），到时自动退出并打印超时拦截面板——不再无限悬挂在
        approval › 提示符上，渲染循环也不会被 input() 卡住导致后续
        流式输出积压。
        """
        body = f"[bold]{escape(req.summary)}[/bold]"
        if req.detail:
            body += f"\n[dim]{escape(req.detail)}[/dim]"
        self.console.print(self._panel(
            body, title=req.title, title_align="left", border_style="yellow"))
        opts = " · ".join(f"{o.key}={o.label}" for o in req.options)
        self.console.print(
            f"[bold yellow]允许执行？[/bold yellow] [dim]{opts}[/dim]")
        deadline = req.deadline or (time.time() + 180)
        keys = "".join(o.key for o in req.options)
        answer = self._read_key_until(deadline, keys)
        if answer:
            return answer
        hint = escape(req.timeout_hint) if req.timeout_hint else ""
        self.console.print(self._panel(
            f"[bold]{escape(req.summary)}[/bold]\n[dim]{hint}[/dim]",
            title="⏰ 审批超时 · 已按拒绝处理",
            title_align="left", border_style="red"))
        return ""

    def _read_key_until(self, deadline: float, keys: str) -> str:
        """在 deadline（unix 时间戳）前读取单键答案，到时返回 ""。

        在 worker 线程中运行。答案本来就是单字符，Windows 终端下用
        msvcrt 轮询按键替代阻塞式 input()，从而可以带 deadline；
        非 Windows / 非终端环境回退到原 input() 行为（这些环境通常
        无人值守，后端会超时 fail-closed）。
        """
        if sys.platform == "win32" and self._use_ptk:
            return self._read_key_win32(deadline, keys)
        while True:
            answer = input("approval › ").strip().lower()
            if answer in keys:
                return answer
            self.console.print(
                f"[yellow]请输入 {' / '.join(keys)}[/yellow]")

    def _read_key_win32(self, deadline: float, keys: str) -> str:
        """msvcrt 单键读取 + 同行倒计时刷新。在 worker 线程中运行。"""
        import msvcrt
        # 丢弃提问前用户预敲的残留按键，避免被误当作答案
        while msvcrt.kbhit():
            msvcrt.getwch()
        last_shown = -1
        while True:
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

    # ------------------------------------------------------------------
    # Standing consumers: demultiplex outbound traffic by turn_id
    # ------------------------------------------------------------------

    async def _stream_listener(self):
        """Standing consumer of the channel's stream queue.

        Events for the active interactive turn are forwarded to
        _render_turn's private queue; everything else (scheduled tasks,
        interrupted turns) goes to the background pipeline. Never blocks
        on the render lock, so the active turn can never starve.
        """
        while True:
            event = await self._stream_queue.dequeue()
            if event.turn_id and event.turn_id == self._active_turn_id:
                await self._turn_events.put(event)
            else:
                self._handle_bg_stream_event(event)

    async def _out_listener(self):
        """Standing consumer of the channel's MessageOut queue."""
        while True:
            msg = await self._out_queue.dequeue()
            turn_id = msg.turn_id
            if turn_id and turn_id == self._active_turn_id:
                await self._turn_out.put(msg)
            else:
                self._finalize_bg_turn(
                    turn_id or msg.id,
                    text=msg.body.content or "",
                    session_id=msg.session_id,
                    origin=msg.origin,
                )

    # ------------------------------------------------------------------
    # Background pipeline: accumulate async turns, render on completion
    # ------------------------------------------------------------------

    def _handle_bg_stream_event(self, event: StreamEvent):
        acc = self._bg_turns.setdefault(event.turn_id, {
            "text": "",
            "session_id": event.session_id,
            "origin": event.origin,
            "buf": "",
            "last_flush": 0.0,
            "streamed": False,
        })
        if event.kind == "delta":
            acc["text"] += event.content
            if self._bg_stream_enabled:
                self._bg_stream_push(acc, event.content)
        elif event.kind == "tool_call":
            if self._bg_stream_enabled:
                self._bg_stream_push(acc, f"\n[调用工具 {event.content}]\n")
        elif event.kind == "approval":
            # turn 已被 Ctrl+C 移入后台：审批请求无法就地提问，后端将
            # 超时 fail-closed。借异步结果管线（自带渲染锁串行）给用户
            # 一条明确提示，而不是静默丢弃
            self._notify_bg_approval(event)
        elif event.kind in ("done", "error"):
            self._finalize_bg_turn(
                event.turn_id,
                text=event.content or acc["text"],
                session_id=acc["session_id"],
                origin=acc["origin"],
                error=event.kind == "error",
                streamed=acc["streamed"],
            )

    def _notify_bg_approval(self, event: StreamEvent):
        """后台 turn 的审批请求无法就地回答，提示用户该命令将按超时拒绝。"""
        try:
            cmd = json.loads(event.content).get("command", "")
        except json.JSONDecodeError:
            cmd = ""
        text = ("有 shell 命令需要人工审批，但当前 turn 已转入后台，无法就地"
                "回答，将按超时自动拒绝。")
        if cmd:
            text += f"\n\n命令：`{cmd}`"
        self._async_results.put_nowait({
            "turn_id": event.turn_id,
            "session_id": event.session_id,
            "text": text,
            "error": False,
            "streamed": False,
            "source": "",
            "task_id": "",
            "kind": "approval_blocked",
        })

    def _print_above_prompt(self, renderable, *, end: str = "\n"):
        """Print a rich renderable above the active input prompt.

        提示符活跃时不走 patch_stdout 代理写原始 ANSI：那条路径曾经把
        转义序列转义成 "?[2m" 乱码（raw=False），raw 直通又绕过了 ptk
        渲染器的属性状态管理，导致面板颜色状态不闭合（黄色"流"出边框）。
        这里把 rich 渲染结果捕获为 ANSI 字符串，交给 ptk 自己的
        print_formatted_text 管线重新渲染——它首尾都会显式
        reset_attributes，保证颜色闭合；run_in_terminal 负责在提示符
        上方输出并重绘提示符。提示符不活跃时直接打印到真实 stdout。
        """
        if self._use_ptk and self._prompt_active:
            with self.console.capture() as capture:
                self.console.print(renderable, end=end)
            ansi = capture.get()
            run_in_terminal(
                lambda: print_formatted_text(PtkANSI(ansi), end=""),
                in_executor=False,
            )
        else:
            self.console.print(renderable, end=end)

    def _bg_stream_push(self, acc: dict, chunk: str):
        """Live-print background turn output above the prompt (/bgstream on).

        Throttled and skipped while an interactive Live render holds the
        render lock — the completion panel will carry the full text then.
        """
        if self._render_lock.locked():
            return
        acc["buf"] += chunk
        now = time.monotonic()
        if (now - acc["last_flush"] < 0.4
                and "\n" not in acc["buf"] and len(acc["buf"]) < 200):
            return
        acc["last_flush"] = now
        text, acc["buf"] = acc["buf"], ""
        if not acc["streamed"]:
            self._print_above_prompt("[dim]⏰ 后台任务输出：[/dim]")
            acc["streamed"] = True
        self._print_above_prompt(Text(text, style="dim"), end="")

    def _finalize_bg_turn(self, turn_id: str, *, text: str, session_id: str,
                          origin: Origin, error: bool = False,
                          streamed: bool = False):
        """Finalize a background turn exactly once (stream done and the
        final MessageOut both arrive; whichever comes first wins)."""
        if not turn_id:
            return
        if len(self._bg_finalized) > 1000:
            self._bg_finalized.clear()
        if turn_id in self._bg_finalized:
            return
        self._bg_finalized.add(turn_id)
        self._bg_turns.pop(turn_id, None)
        text = (text or "").strip()
        if not text:
            return
        self._async_results.put_nowait({
            "turn_id": turn_id,
            "session_id": session_id,
            "text": text,
            "error": error,
            "streamed": streamed,
            "source": origin.kind,
            "task_id": origin.task_id or "",
        })

    async def _async_render_worker(self):
        """Render completed background results one at a time, serialized
        with interactive Live rendering via the render lock."""
        while True:
            rec = await self._async_results.get()
            async with self._render_lock:
                self._render_async_record(rec)
            self._unread += 1
            if rec["source"] == "scheduler":
                toast("zero · 定时任务完成", rec["text"])
            elif rec.get("kind") == "approval_blocked":
                toast("zero · 审批未受理", rec["text"])

    def _render_async_record(self, rec: dict):
        subtitle_parts = []
        title = SessionStore.get_title(CHANNEL, rec["session_id"])
        if title:
            subtitle_parts.append(f"会话「{escape(title)}」")
        if rec["source"] == "scheduler" and rec["task_id"]:
            task = SchedulerStore.get(rec["task_id"])
            if task is not None:
                instruction = task.instruction.removeprefix("[Scheduled Task]")
                subtitle_parts.append(escape(self._clip(instruction, 40)))
        subtitle = " · ".join(subtitle_parts) or None

        if rec.get("kind") == "approval_blocked":
            self._print_above_prompt(self._panel(
                Markdown(rec["text"]), title="zero · 审批未受理",
                title_align="left", border_style="red", subtitle=subtitle))
        elif rec["error"]:
            self._print_above_prompt(self._panel(
                escape(rec["text"]), title="zero · 定时任务出错",
                title_align="left", border_style="red", subtitle=subtitle))
        elif rec["source"] == "scheduler":
            if rec["streamed"]:
                # /bgstream 模式下正文已流式输出，只补一行完成提示
                self._print_above_prompt(
                    f"\n[green]✅ 定时任务完成[/green]"
                    + (f" [dim]{subtitle}[/dim]" if subtitle else ""))
                return
            self._print_above_prompt(self._panel(
                Markdown(rec["text"]), title="zero · 定时任务",
                title_align="left", border_style="yellow", subtitle=subtitle))
        else:
            self._print_above_prompt(self._panel(
                Markdown(rec["text"]), title="zero · 后台回答",
                title_align="left", border_style="blue", subtitle=subtitle))
