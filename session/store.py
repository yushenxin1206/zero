"""
Session storage module.

Persists conversation messages as JSONL files:
    ~/.zero/sessions/{channel}/{date}/{session_id}/messages.jsonl

Channel-level index for session lookup:
    ~/.zero/sessions/{channel}/index.json
    {
        "<source_id>": {
            "session_id": "<uuid4>",
            "date": "2026-08-12",
            "last_updated_at": "2026-08-12T14:30:00"
        }
    }
"""
import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from datacls import Message

SESSIONS_ROOT = Path.home() / ".zero" / "sessions"


class SessionStore:
    """Append-only JSONL storage for conversation sessions."""

    # ------------------------------------------------------------------
    # Index file helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _index_path(channel: str) -> Path:
        return SESSIONS_ROOT / channel / "index.json"

    @staticmethod
    def _load_index(channel: str) -> dict:
        path = SessionStore._index_path(channel)
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def _save_index(channel: str, index: dict) -> None:
        path = SessionStore._index_path(channel)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)

    @staticmethod
    def _find_entry_by_session_id(channel: str, session_id: str) -> dict | None:
        index = SessionStore._load_index(channel)
        for entry in index.values():
            if entry["session_id"] == session_id:
                return entry
        return None

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _session_dir(channel: str, session_id: str) -> Path:
        entry = SessionStore._find_entry_by_session_id(channel, session_id)
        if entry is not None:
            date = entry["date"]
        else:
            # Session not in index (e.g. source_id was empty at creation
            # time). Fall back to today's date.
            date = datetime.now().strftime("%Y-%m-%d")
        return SESSIONS_ROOT / channel / date / session_id

    @staticmethod
    def _session_file(channel: str, session_id: str) -> Path:
        return SessionStore._session_dir(channel, session_id) / "messages.jsonl"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def resolve_session(channel: str, source_id: str) -> str:
        """
        Look up or create a session for the given channel-specific source_id.

        Returns the session_id (uuid4). A new session is created on first
        contact; subsequent calls with the same source_id return the existing
        session_id.
        """
        if not source_id:
            return str(uuid4())

        index = SessionStore._load_index(channel)
        if source_id in index:
            entry = index[source_id]
            entry["last_updated_at"] = datetime.now().isoformat()
            SessionStore._save_index(channel, index)
            return entry["session_id"]

        session_id = str(uuid4())
        today = datetime.now().strftime("%Y-%m-%d")
        now = datetime.now().isoformat()
        index[source_id] = {
            "session_id": session_id,
            "date": today,
            "last_updated_at": now,
        }
        SessionStore._save_index(channel, index)
        return session_id

    @staticmethod
    def append(message: Message) -> None:
        """Append a Message to its session file."""
        path = SessionStore._session_file(message.channel, message.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(message.model_dump_json() + "\n")
        SessionStore._touch_last_updated(message.channel, message.session_id)

    @staticmethod
    def _touch_last_updated(channel: str, session_id: str) -> None:
        index = SessionStore._load_index(channel)
        for entry in index.values():
            if entry["session_id"] == session_id:
                entry["last_updated_at"] = datetime.now().isoformat()
                SessionStore._save_index(channel, index)
                return

    @staticmethod
    def load_history(session_id: str, channel: str) -> list[Message]:
        """Load all messages for a session in chronological order."""
        try:
            path = SessionStore._session_file(channel, session_id)
        except ValueError:
            return []
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

