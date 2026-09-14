"""
Interactive prompt for the CLI channel, built on prompt_toolkit.

Features:
  - Typing '/' immediately brings up the slash-command completion menu,
    sorted by usage frequency (most-used first).
  - An inline autosuggestion shows the most frequently used command for
    the current '/' prefix (accept with Right-Arrow or Ctrl+E).
  - Tab / arrow keys navigate the completion menu.

Usage counts persist to ~/.zero/sessions/cli/.cli_stats.json so the
ranking survives restarts.
"""
import json
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggest, Suggestion
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.history import InMemoryHistory

from session.store import SESSIONS_ROOT

STATS_PATH = SESSIONS_ROOT / "cli" / ".cli_stats.json"

# Canonical slash commands (keep in sync with CLIChannel._handle_command)
COMMANDS: list[tuple[str, str]] = [
    ("/new", "新建会话"),
    ("/sessions", "选择 / 切换历史会话"),
    ("/title", "设置 / 查看当前会话标题"),
    ("/tasks", "查看定时任务（/tasks all 查看全部）"),
    ("/bgstream", "开关定时任务结果的流式输出"),
    ("/history", "分页查看本会话全部历史"),
    ("/help", "显示可用命令"),
    ("/quit", "退出 CLI"),
]


class CommandStats:
    """Persistent per-command usage counts used for ranking."""

    def __init__(self, path: Path = STATS_PATH):
        self._path = path
        self._counts: dict[str, int] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._counts = {str(k): int(v) for k, v in data.items()}
            except (OSError, ValueError):
                self._counts = {}

    def record(self, cmd: str):
        self._counts[cmd] = self._counts.get(cmd, 0) + 1
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._counts, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def ranked(self, prefix: str = "") -> list[str]:
        """Command names matching prefix, most-used first, then alphabetical."""
        names = [c for c, _ in COMMANDS if c.startswith(prefix)]
        return sorted(names, key=lambda c: (-self._counts.get(c, 0), c))


class SlashCommandCompleter(Completer):
    """Completes slash commands; typing '/' lists all commands."""

    def __init__(self, stats: CommandStats):
        self._stats = stats

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/") or " " in text:
            return
        descriptions = dict(COMMANDS)
        for cmd in self._stats.ranked(text):
            yield Completion(
                cmd,
                start_position=-len(text),
                display=cmd,
                display_meta=descriptions.get(cmd, ""),
            )


class SlashAutoSuggest(AutoSuggest):
    """Inline-suggests the most-used command matching the '/' prefix."""

    def __init__(self, stats: CommandStats):
        self._stats = stats

    def get_suggestion(self, buffer, document):
        text = document.text_before_cursor
        if text.startswith("/"):
            candidates = [c for c in self._stats.ranked(text) if c != text]
            if candidates:
                return Suggestion(candidates[0][len(text):])
        return None


def build_prompt_session(stats: CommandStats | None = None) -> PromptSession:
    stats = stats or CommandStats()
    return PromptSession(
        history=InMemoryHistory(),
        completer=SlashCommandCompleter(stats),
        auto_suggest=SlashAutoSuggest(stats),
        complete_while_typing=True,
    )
