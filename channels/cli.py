"""
CLI channel module — interactive terminal chat powered by rich.

Features:
  1. Session picker on startup: continue latest / pick a historical
     session / create a new one (channel = "cli").
  2. Real-time streaming rendering of the assistant reply (Live + Markdown),
     with tool-call status indicators.
  3. Slash commands: /new /sessions /history /help /quit

Input runs in a worker thread (blocking input()) while the event loop
keeps consuming stream events, so rendering stays live.
"""
import asyncio
import json
import logging
import sys
from uuid import uuid4

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Prompt
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from bus.queue import MessageBus
from channels.cli_prompt import COMMANDS, CommandStats, build_prompt_session
from datacls import MessageBody, MessageIn, SessionMeta
from prompt_toolkit.styles import Style
from session.store import SESSIONS_ROOT, SessionStore

logger = logging.getLogger(__name__)

CHANNEL = "cli"
BINDING_PATH = SESSIONS_ROOT / CHANNEL / ".cli_current.json"

# 输入提示符样式（bold cyan），与历史消息中的 "you ›" 一致；
# 只作用于 class:prompt 片段，不影响用户输入的文字
_PROMPT_STYLE = Style.from_dict({"prompt": "bold ansicyan"})

# alias -> canonical command (canonical names defined in cli_prompt.COMMANDS)
_COMMAND_ALIASES = {
    "/n": "/new", "/s": "/sessions", "/h": "/history", "/t": "/title",
    "/?": "/help", "/q": "/quit", "/exit": "/quit",
}


class CLIChannel:
    """
    Interactive CLI channel.

    Usage:
        channel = CLIChannel(msg_bus)
        await channel.start()   # runs the menu + chat loop, returns on /quit
    """

    def __init__(self, msg_bus: MessageBus):
        self.msg_bus = msg_bus
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
        self.console.print(Panel(
            "[bold]zero[/bold] · cli channel\n"
            f"session [dim]{self.session_id[:8]}[/dim]{title_part} · "
            "commands: /new /sessions /title /history /help /quit",
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

    def _assistant_panel(self, text: str):
        body: object = Markdown(text) if text else Text("(thinking ...)", style="dim")
        return Panel(
            body, title="zero",
            title_align="left", border_style="green",
        )

    def _render_message_line(self, m, max_len: int = 600):
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
        with self.console.pager():
            for m in history:
                if m.sender == "tool":
                    self.console.print(
                        f"[dim]tool › {self._clip(m.body.content or '', 300)}[/dim]")
                else:
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
        await self._chat_loop()

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
        """
        if self._prompt is not None:
            return await self._prompt.prompt_async(
                [("class:prompt", "you › ")], style=_PROMPT_STYLE)
        # 非终端回退：输入无 prompt，提交后用 rich 打印高亮行，与历史一致
        text = await asyncio.to_thread(input, "")
        self.console.print(f"[bold cyan]you ›[/bold cyan] {text}")
        return text

    async def _chat_loop(self):
        while True:
            try:
                # Ctrl+C / Ctrl+D 均视为退出
                user_text = await self._read_input()
            except (EOFError, KeyboardInterrupt):
                self.console.print("\n[dim]bye[/dim]")
                return

            text = (user_text or "").strip()
            if not text:
                continue
            if text.startswith("/"):
                if not self._handle_command(text):
                    return
                continue

            msg = MessageIn(
                channel=CHANNEL,
                session_id=self.session_id,
                body=MessageBody(content=text),
                metadata={
                    "message_id": str(uuid4()),
                    "sender_id": self._source_id,
                },
            )
            await self.msg_bus.in_queue.enqueue(msg)
            try:
                await self._render_turn(msg.id)
            except asyncio.CancelledError:
                # Ctrl+C during generation: abort rendering; the agent keeps
                # running in the background and leftover events are skipped
                # by turn_id in future renders.
                self.console.print(
                    "\n[yellow]已中断渲染，回答仍在后台生成[/yellow]")

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

    def _handle_command(self, text: str) -> bool:
        """Handle a slash command. Returns False to exit the chat loop."""
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
        """Consume stream events for one turn and render them live."""
        acc = ""
        status = ""
        live = Live(
            self._frame(acc, status),
            console=self.console,
            refresh_per_second=12,
            vertical_overflow="visible",
        )
        live.start()
        done = False
        try:
            while not done:
                event = await self._stream_queue.dequeue()
                if event.turn_id != turn_id or event.session_id != self.session_id:
                    # leftover from an interrupted turn or another session — skip
                    continue
                if event.kind == "delta":
                    acc += event.content
                elif event.kind == "tool_call":
                    status = f"调用工具 {self._clip(event.content, 90)}"
                elif event.kind == "error":
                    live.stop()
                    self.console.print(f"[red]出错:[/red] {event.content}")
                    return
                elif event.kind == "done":
                    if event.content and not acc:
                        acc = event.content
                    done = True
                live.update(self._frame(acc, status))
        finally:
            live.stop()
            self.console.print()

        # Drain the matching final MessageOut so queues stay in sync.
        try:
            await asyncio.wait_for(self._out_queue.dequeue(), timeout=2)
        except (asyncio.TimeoutError, TimeoutError):
            pass
