"""
Channel-agnostic services shared by every transport.

Channels are dumb adapters on purpose: they render what these methods
return and translate transport input into these method calls. Session
listing rules ("empty sessions disappear"), title derivation, task
filtering, model switching and turn submission are defined **once** here
instead of being re-implemented per channel.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from zero.bus.queue import MessageBus
from zero.config import Config
from zero.core.model_switch import ModelSwitchResult, session_model, switch_model
from zero.models import Message, MessageBody, MessageIn, StopRequest
from zero.scheduler.models import ScheduleTask
from zero.scheduler.store import SchedulerStore
from zero.session.models import SessionMeta
from zero.session.store import SessionStore
from zero.tools.registry import ToolRegistry


@dataclass(frozen=True)
class SessionView:
    """A session as a channel displays it.

    ``title`` is already resolved: the stored title, or a derived one from
    the first user message. ``title_is_derived`` tells the renderer whether
    to dim it.
    """

    session_id: str
    source_id: str
    title: str
    title_is_derived: bool
    date: str
    last_updated_at: datetime


class ChannelService:
    """Per-channel domain operations: sessions, history, tasks, models, turns.

    ``channel`` doubles as the on-disk bucket name, so each transport keeps
    its own binding file and never overwrites another transport's session.
    """

    def __init__(
        self,
        channel: str,
        *,
        msg_bus: MessageBus,
        session_store: SessionStore,
        scheduler_store: SchedulerStore,
        config: Config | None = None,
        tool_registry: ToolRegistry | None = None,
        source_prefix: str | None = None,
    ):
        self.channel = channel
        self.msg_bus = msg_bus
        self.config = config
        self.tool_registry = tool_registry
        self.source_prefix = source_prefix or channel
        self.source_id = ""
        self.session_id = ""
        # 会话存储由 channel 组合根显式传入；绑定文件与 session 同根
        self.session_store = session_store
        self.scheduler_store = scheduler_store
        self._binding_path = (
            session_store.root / channel / f".{channel}_current.json")

    # ------------------------------------------------------------------
    # Session binding
    # ------------------------------------------------------------------

    def restore_binding(self) -> bool:
        """Load the persisted binding. Returns False when absent/corrupt."""
        try:
            data = json.loads(self._binding_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        source_id = data.get("source_id")
        session_id = data.get("session_id")
        if not source_id or not session_id:
            return False
        self._bind(str(source_id), str(session_id), persist=False)
        return True

    def bind(self, source_id: str, session_id: str) -> None:
        """Point this channel at an existing session and persist the choice."""
        self._bind(source_id, session_id, persist=True)

    def new_session(self) -> str:
        """Create and bind a fresh session for this channel."""
        source_id = f"{self.source_prefix}-{uuid4().hex[:8]}"
        session_id = self.session_store.create_session(
            self.channel, source_id)
        self._bind(source_id, session_id, persist=True)
        return session_id

    def _bind(self, source_id: str, session_id: str, *, persist: bool) -> None:
        self.source_id = source_id
        self.session_id = session_id
        if not persist:
            return
        self._binding_path.parent.mkdir(parents=True, exist_ok=True)
        self._binding_path.write_text(json.dumps({
            "source_id": source_id,
            "session_id": session_id,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def sessions(self) -> list[SessionView]:
        """List displayable sessions, most recently updated first.

        Sessions created but never used are excluded here — this is the one
        place the "empty sessions don't exist" rule lives.
        """
        views = []
        for entry in self.session_store.list_sessions(self.channel):
            if not self.session_store.has_messages(
                    self.channel, entry.session_id, entry):
                continue
            views.append(self._view(entry))
        return views

    def find(self, session_id: str) -> SessionView | None:
        """Look up one listed session by id (None if missing or empty)."""
        for entry in self.session_store.list_sessions(self.channel):
            if entry.session_id != session_id:
                continue
            if not self.session_store.has_messages(
                    self.channel, entry.session_id, entry):
                return None
            return self._view(entry)
        return None

    def _view(self, entry: SessionMeta) -> SessionView:
        title = entry.title
        derived = not title
        if derived:
            title = self.session_store.make_title(
                self.session_store.first_user_message(
                    self.channel, entry.session_id, entry))
        return SessionView(
            session_id=entry.session_id,
            source_id=entry.source_id,
            title=title,
            title_is_derived=derived,
            date=entry.date,
            last_updated_at=entry.last_updated_at,
        )

    def title(self, session_id: str | None = None) -> str:
        """Stored title for a session ("" when unset)."""
        return self.session_store.get_title(
            self.channel, session_id or self.session_id)

    def rename(self, title: str, session_id: str | None = None) -> bool:
        """Set a session title. Returns False when the session is not indexed."""
        return self.session_store.set_title(
            self.channel, session_id or self.session_id, title)

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def history(self, session_id: str | None = None) -> list[Message]:
        """Full persisted history (including tool messages and compaction)."""
        return self.session_store.load_history(
            session_id or self.session_id, self.channel)

    def display_history(self, session_id: str | None = None) -> list[Message]:
        """Conversation messages a user can be shown (no tool traffic)."""
        return [
            m for m in self.history(session_id)
            if m.sender in ("user", "assistant") and m.body.content
        ]

    # ------------------------------------------------------------------
    # Scheduled tasks
    # ------------------------------------------------------------------

    def tasks(self, *, all_sessions: bool = False) -> list[ScheduleTask]:
        """This channel's tasks for the current session (or all sessions)."""
        tasks = [
            t for t in self.scheduler_store.load_all()
            if t.channel == self.channel
            and (all_sessions or t.session_id == self.session_id)
        ]
        return sorted(
            tasks, key=lambda t: (t.trigger_at is None,
                                  t.trigger_at or t.created_at))

    def task(self, task_id: str) -> ScheduleTask | None:
        """Look up a single scheduled task by id."""
        return self.scheduler_store.get(task_id)

    # ------------------------------------------------------------------
    # Models
    # ------------------------------------------------------------------

    def models(self) -> list[str]:
        """Registered model keys (empty without config)."""
        return sorted(self.config.models) if self.config is not None else []

    def current_model(self) -> str:
        """The model this session resolves to ("" without config, "?" on error)."""
        if self.config is None:
            return ""
        try:
            return session_model(
                self.config, self.channel, self.session_id,
                self.session_store)
        except ValueError:
            return "?"

    async def switch_model(self, name: str) -> ModelSwitchResult:
        """Switch the session-bound model (may compact first)."""
        if self.config is None:
            raise ValueError("未注入配置，无法切换模型")
        tools = (
            self.tool_registry.schemas()
            if self.tool_registry is not None else None)
        return await switch_model(
            config=self.config, channel=self.channel,
            session_id=self.session_id, new_model=name,
            session_store=self.session_store, tools=tools)

    # ------------------------------------------------------------------
    # Turns
    # ------------------------------------------------------------------

    def make_input(self, text: str) -> MessageIn:
        """Build the inbound message for one user turn in the current session."""
        return MessageIn(
            channel=self.channel,
            session_id=self.session_id,
            body=MessageBody(content=text),
            channel_message_id=str(uuid4()),
            channel_sender_id=self.source_id,
            channel_session_id=self.source_id,
        )

    async def submit(self, text: str) -> MessageIn:
        """Enqueue a user turn and return the message that identifies it."""
        message = self.make_input(text)
        await self.msg_bus.in_queue.enqueue(message)
        return message

    async def request_stop(self, turn_id: str) -> None:
        """Ask the core to stop one turn (bypasses the serial in-queue)."""
        await self.msg_bus.stop_queue.enqueue(StopRequest(
            channel=self.channel, session_id=self.session_id, turn_id=turn_id))
