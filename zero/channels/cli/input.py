"""
Terminal input for the CLI channel.

Owns the prompt_toolkit session (slash-command completion + autosuggestion),
the piped-stdin fallback for non-TTY environments (tests, CI, shell pipes),
and the unread badge shown while background results are waiting.

Reading input is blocking; the channel runs it on an awaitable worker so the
event loop keeps consuming stream events and the interface stays live.
"""
from __future__ import annotations

import asyncio
import sys
from typing import Callable

from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from rich.console import Console

from zero.channels.cli.prompt import CommandStats, build_prompt_session

# 输入提示符样式（bold cyan），与历史消息中的 "you ›" 一致；
# 只作用于 class:prompt 片段，不影响用户输入的文字
_PROMPT_STYLE = Style.from_dict({"prompt": "bold ansicyan"})


class CliInput:
    """Reads one line at a time, keeping the prompt state observable."""

    def __init__(self, console: Console, *, toolbar: Callable[[], str]):
        self.console = console
        self._toolbar = toolbar
        self.stats = CommandStats()
        # prompt_toolkit needs a real console — fall back to plain input()
        # when stdin/stdout are piped (e.g. CI, tests, shell pipes)
        self._use_ptk = sys.stdin.isatty() and sys.stdout.isatty()
        self._prompt = build_prompt_session(self.stats) if self._use_ptk else None
        self._active = False
        self._unread = 0

    @property
    def active(self) -> bool:
        """True while prompt_toolkit is awaiting input."""
        return self._active

    @property
    def uses_ptk(self) -> bool:
        """True when the interactive prompt_toolkit path is available."""
        return self._use_ptk

    @property
    def unread(self) -> int:
        return self._unread

    def mark_unread(self) -> None:
        self._unread += 1

    def mark_read(self) -> None:
        self._unread = 0

    def record_command(self, name: str) -> None:
        self.stats.record(name)

    async def read(self) -> str:
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
            self._active = True
            try:
                with patch_stdout(raw=True):
                    return await self._prompt.prompt_async(
                        [("class:prompt", prompt_text)],
                        style=_PROMPT_STYLE,
                        bottom_toolbar=self._toolbar(),
                    )
            finally:
                self._active = False
        # 非终端回退：输入无 prompt，提交后用 rich 打印高亮行，与历史一致
        text = await asyncio.to_thread(input, "")
        self.console.print(f"[bold cyan]you ›[/bold cyan] {text}")
        return text
