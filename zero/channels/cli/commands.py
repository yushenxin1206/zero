"""
Slash-command control surface for the CLI channel.

Session selection (startup and ``/sessions``), renaming, model switching,
task listing, background-stream toggling and history display are user
intents layered on top of ``ChannelService``. They live together here so
``CLIChannel`` stays a thin lifecycle/demux shell.
"""
from __future__ import annotations

import asyncio

from rich.console import Console
from rich.markup import escape
from rich.prompt import Prompt
from rich.table import Table

from zero.channels.cli.input import CliInput
from zero.channels.cli.prompt import COMMANDS
from zero.channels.cli.renderer import CliRenderer
from zero.channels.service import ChannelService

# alias -> canonical command (canonical names defined in prompt.COMMANDS)
_COMMAND_ALIASES = {
    "/n": "/new", "/s": "/sessions", "/h": "/history", "/t": "/title",
    "/ts": "/tasks", "/?": "/help", "/q": "/quit", "/exit": "/quit",
}


class CliCommands:
    """Interprets slash commands and the session picker."""

    def __init__(self, console: Console, service: ChannelService,
                 renderer: CliRenderer, input_: CliInput):
        self.console = console
        self.service = service
        self.renderer = renderer
        self.input = input_
        # /bgstream toggle: stream background turn deltas live above the prompt
        self.bg_stream_enabled = False

    # ------------------------------------------------------------------
    # Session selection
    # ------------------------------------------------------------------

    def pick_session(self) -> bool:
        """Show the session menu. Returns False if the user chose to quit."""
        while True:
            sessions = self.service.sessions()
            if sessions:
                self.console.print(
                    self.renderer.session_table(sessions, self.service.session_id))
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
                    self.service.bind(entry.source_id, entry.session_id)
                else:
                    self.service.new_session()
                return True
            if choice in ("n", "new"):
                self.service.new_session()
                return True
            if choice.isdigit() and 1 <= int(choice) <= min(len(sessions), 20):
                entry = sessions[int(choice) - 1]
                self.service.bind(entry.source_id, entry.session_id)
                return True
            self.console.print("[yellow]无效输入，请重试[/yellow]")

    def confirm_or_switch(self) -> bool:
        """After restoring a binding, let the user enter the session or re-pick."""
        choice = Prompt.ask(
            "[bold cyan]回车进入会话[/bold cyan]，输入 s 重新选择，q 退出",
            default="",
        ).strip().lower()
        if choice in ("q", "quit"):
            return False
        if choice in ("s", "sessions", "l", "list"):
            return self.pick_session()
        return True

    def greet_current(self) -> None:
        """Print the banner and recent history for the active session."""
        self.renderer.print_banner(
            self.service.session_id, self.service.title())
        self.renderer.print_recent_history(self.service.session_id)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def dispatch(self, text: str) -> bool:
        """Handle one slash command. Returns False when the CLI should exit."""
        cmd, _, arg = text.partition(" ")
        cmd = cmd.lower()
        canonical = _COMMAND_ALIASES.get(cmd, cmd)
        if canonical in {c for c, _ in COMMANDS}:
            self.input.record_command(canonical)
        if cmd in ("/quit", "/exit", "/q"):
            self.console.print("[dim]bye[/dim]")
            return False
        if cmd in ("/new", "/n"):
            # 新建会话要写索引，打招呼要读历史：都放线程池
            await asyncio.to_thread(self.service.new_session)
            await asyncio.to_thread(self.greet_current)
            return True
        if cmd in ("/sessions", "/s"):
            if self.pick_session():
                await asyncio.to_thread(self.greet_current)
            return True
        if cmd in ("/title", "/t"):
            # 索引读写是阻塞 IO，放到线程池，避免卡住同一事件循环里的流式渲染
            await asyncio.to_thread(self._handle_title, arg)
            return True
        if cmd in ("/model", "/m"):
            await self._handle_model(arg)
            return True
        if cmd in ("/tasks", "/ts"):
            await asyncio.to_thread(self._handle_tasks, arg)
            return True
        if cmd == "/bgstream":
            self._handle_bgstream(arg)
            return True
        if cmd in ("/history", "/h"):
            await asyncio.to_thread(
                self.renderer.print_full_history, self.service.session_id)
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
    # Individual commands
    # ------------------------------------------------------------------

    def _handle_title(self, arg: str) -> None:
        """`/title` shows the current title; `/title <text>` renames."""
        arg = arg.strip()
        if not arg:
            current = self.service.title()
            if current:
                self.console.print(f"当前标题: [bold]{escape(current)}[/bold]")
            else:
                self.console.print(
                    "[dim]当前会话暂无标题，用法: /title <新标题>[/dim]")
            return
        if self.service.rename(arg):
            self.console.print(f"[green]标题已更新:[/green] {escape(arg)}")
        else:
            self.console.print("[yellow]当前会话不在索引中，无法命名[/yellow]")

    def _handle_tasks(self, arg: str) -> None:
        """`/tasks` 列出当前会话的定时任务；`/tasks all` 列出 cli 渠道全部。"""
        show_all = arg.strip().lower() == "all"
        tasks = self.service.tasks(all_sessions=show_all)
        if not tasks:
            hint = "" if show_all else "（/tasks all 查看全部）"
            self.console.print(f"[dim]暂无定时任务{hint}[/dim]")
            return

        status_style = {
            "pending": "yellow", "running": "green",
            "completed": "dim", "failed": "red", "cancelled": "dim",
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

        for task in tasks:
            style = status_style.get(task.status, "")
            instruction = task.instruction.removeprefix("[Scheduled Task]")
            table.add_row(
                f"[{style}]{task.status}[/{style}]" if style else task.status,
                task.schedule_type + (f" {task.cron}" if task.cron else ""),
                _fmt(task.trigger_at),
                _fmt(task.last_run_at),
                escape(self.renderer.clip(instruction, 40)),
                task.id[:8],
            )
        self.console.print(table)

    def _handle_bgstream(self, arg: str) -> None:
        """`/bgstream` 切换定时任务结果的流式输出；`/bgstream on|off` 显式设置。"""
        arg = arg.strip().lower()
        if arg == "on":
            self.bg_stream_enabled = True
        elif arg == "off":
            self.bg_stream_enabled = False
        else:
            self.bg_stream_enabled = not self.bg_stream_enabled
        if self.bg_stream_enabled:
            self.console.print(
                "[green]定时任务流式输出已开启[/green] "
                "[dim]（任务输出将实时打印在输入行上方）[/dim]")
        else:
            self.console.print(
                "[green]定时任务流式输出已关闭[/green] "
                "[dim]（任务完成后一次性展示结果）[/dim]")

    async def _handle_model(self, arg: str) -> None:
        """查看 / 切换本会话模型（候选来自 config.models 注册表）。"""
        models = self.service.models()
        if self.service.config is None:
            self.console.print("[yellow]未注入配置，无法切换模型[/yellow]")
            return
        if not models:
            self.console.print("[dim]models 注册表为空[/dim]")
            return
        current = await asyncio.to_thread(self.service.current_model)
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
            result = await self.service.switch_model(name)
        except ValueError as exc:
            self.console.print(f"[red]{exc}[/red]")
            return
        if not result.switched:
            self.console.print(f"[dim]当前已是 {result.new_model}，无需切换[/dim]")
            return
        note = " · [bold green]已按新窗口压缩会话[/]" if result.compacted else ""
        self.console.print(
            f"[green]模型已切换[/green] {result.old_model} → "
            f"[bold]{result.new_model}[/bold]（窗口 {result.window_tokens}）{note}")
