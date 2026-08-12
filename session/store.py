"""
Session storage module.

Persists conversation messages as JSONL files:
    ~/.zero/sessions/{channel}/{session_id}.jsonl

Each line is a Message JSON, appended in chronological order.
"""
import re
from pathlib import Path

from datacls import Message


SESSIONS_ROOT = Path.home() / ".zero" / "sessions"

# Filesystem-unsafe characters to sanitize in session_id
_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|]')


def _sanitize(name: str) -> str:
    """Replace filesystem-unsafe characters in session_id for use as filename."""
    return _UNSAFE_CHARS.sub("_", name)


class SessionStore:
    """Append-only JSONL storage for conversation sessions."""

    @staticmethod
    def _session_file(session_id: str, channel: str) -> Path:
        safe_id = _sanitize(session_id)
        return SESSIONS_ROOT / channel / f"{safe_id}.jsonl"

    @staticmethod
    def append(message: Message) -> None:
        """Append a Message to its session file."""
        path = SessionStore._session_file(message.session_id, message.channel)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(message.model_dump_json() + "\n")

    @staticmethod
    def load_history(session_id: str, channel: str) -> list[Message]:
        """Load all messages for a session in chronological order."""
        path = SessionStore._session_file(session_id, channel)
        if not path.exists():
            return []
        messages = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                messages.append(Message.model_validate_json(line))
        return messages
