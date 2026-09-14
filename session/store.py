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
            "last_updated_at": "2026-08-12T14:30:00",
            "title": "调试飞书消息丢失问题"
        }
    }

Index entries are modeled by datacls.SessionMeta. Fields missing in older
index files (e.g. title) fall back to model defaults; unknown fields are
ignored on validation. The on-disk format stays snake_case JSON.
"""
import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from datacls import Message, SessionMeta

SESSIONS_ROOT = Path.home() / ".zero" / "sessions"

# 自动标题（首条用户消息截断）的最大长度
TITLE_MAX_LEN = 30


class SessionStore:
    """Append-only JSONL storage for conversation sessions."""

    # ------------------------------------------------------------------
    # Index file helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _index_path(channel: str) -> Path:
        return SESSIONS_ROOT / channel / "index.json"

    @staticmethod
    def _load_index(channel: str) -> dict[str, SessionMeta]:
        path = SessionStore._index_path(channel)
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        return {sid: SessionMeta.model_validate(e) for sid, e in raw.items()}

    @staticmethod
    def _save_index(channel: str, index: dict[str, SessionMeta]) -> None:
        path = SessionStore._index_path(channel)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {sid: e.model_dump(mode="json") for sid, e in index.items()}
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @staticmethod
    def _find_entry_by_session_id(channel: str, session_id: str) -> SessionMeta | None:
        index = SessionStore._load_index(channel)
        for entry in index.values():
            if entry.session_id == session_id:
                return entry
        return None

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _session_dir(channel: str, session_id: str) -> Path:
        entry = SessionStore._find_entry_by_session_id(channel, session_id)
        if entry is not None:
            date = entry.date
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
            entry.last_updated_at = datetime.now()
            SessionStore._save_index(channel, index)
            return entry.session_id

        meta = SessionMeta(date=datetime.now().strftime("%Y-%m-%d"))
        index[source_id] = meta
        SessionStore._save_index(channel, index)
        return meta.session_id

    @staticmethod
    def create_session(channel: str, source_id: str) -> str:
        """
        Force-create a NEW session for source_id, replacing any existing
        binding. Use for explicit "new conversation" actions (CLI /new).
        """
        index = SessionStore._load_index(channel)
        meta = SessionMeta(date=datetime.now().strftime("%Y-%m-%d"))
        index[source_id] = meta
        SessionStore._save_index(channel, index)
        return meta.session_id

    @staticmethod
    def list_sessions(channel: str) -> list[SessionMeta]:
        """
        List all sessions of a channel, most recently updated first.

        Each returned SessionMeta has source_id filled in from the index key.
        """
        index = SessionStore._load_index(channel)
        entries = []
        for sid, entry in index.items():
            entry.source_id = sid
            entries.append(entry)
        entries.sort(key=lambda e: e.last_updated_at, reverse=True)
        return entries

    # ------------------------------------------------------------------
    # Session titles
    # ------------------------------------------------------------------

    @staticmethod
    def make_title(text: str) -> str:
        """Derive a session title from a message (whitespace-collapsed,
        truncated to TITLE_MAX_LEN)."""
        title = " ".join((text or "").split())
        if len(title) > TITLE_MAX_LEN:
            title = title[:TITLE_MAX_LEN] + "…"
        return title

    @staticmethod
    def get_title(channel: str, session_id: str) -> str:
        """Return the session's title, or "" if unset/unknown."""
        entry = SessionStore._find_entry_by_session_id(channel, session_id)
        return entry.title if entry else ""

    @staticmethod
    def set_title(channel: str, session_id: str, title: str) -> bool:
        """Manually set/rename a session title. Returns True if updated."""
        index = SessionStore._load_index(channel)
        for entry in index.values():
            if entry.session_id == session_id:
                entry.title = title.strip()
                SessionStore._save_index(channel, index)
                return True
        return False

    @staticmethod
    def auto_title(channel: str, session_id: str, text: str) -> None:
        """Set the title from the first user message, unless one is already
        set (auto or manual). No-op for sessions not in the index."""
        title = SessionStore.make_title(text)
        if not title:
            return
        index = SessionStore._load_index(channel)
        for entry in index.values():
            if entry.session_id == session_id:
                if not entry.title:
                    entry.title = title
                    SessionStore._save_index(channel, index)
                return

    @staticmethod
    def first_user_message(channel: str, session_id: str) -> str:
        """Return the content of the session's first user message ("" if
        none). Stops at the first match without loading the whole file."""
        try:
            path = SessionStore._session_file(channel, session_id)
        except ValueError:
            return ""
        if not path.exists():
            return ""
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    m = Message.model_validate_json(line)
                except ValueError:
                    continue
                if m.sender == "user" and m.body.content:
                    return m.body.content
        return ""

    @staticmethod
    def has_messages(channel: str, session_id: str) -> bool:
        """True if the session has a non-empty messages file."""
        try:
            path = SessionStore._session_file(channel, session_id)
        except ValueError:
            return False
        return path.exists() and path.stat().st_size > 0

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
            if entry.session_id == session_id:
                entry.last_updated_at = datetime.now()
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
