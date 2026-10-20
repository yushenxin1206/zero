"""
Web channel — local WebSocket server for the pywebview desktop UI.

The channel is deliberately transport-only: it translates between the
MessageBus and JSON frames on WebSocket connections. All agent behavior
(turns, tools, approvals, scheduled tasks) stays in the core.

Protocol (JSON text frames):

  client → server
    {"type": "send", "text": "..."}               send a user message
    {"type": "stop", "turnId": "..."}             cancel the active turn
    {"type": "approval", "reqId": "...", "answer": "y"}
    {"type": "sessions"}                          list sessions
    {"type": "history", "sessionId": "..."}       load a session history
    {"type": "select", "sessionId": "..."}        switch current session
    {"type": "new"}                               create a new session
    {"type": "title", "title": "..."}             rename current session
    {"type": "model", "model": ""}                query / switch model
    {"type": "tasks"}                             list scheduled tasks

  server → client
    {"type": "state", ...}         current session / sessions / model
    {"type": "history", ...}       session messages
    {"type": "stream", "event": {...}}   StreamEvent (delta/reasoning/...)
    {"type": "out", "message": {...}}    final MessageOut
    {"type": "sessions", ...}      session list refresh
    {"type": "tasks", ...}
    {"type": "notice", "level": "info"|"error", "text": "..."}
"""
import asyncio
import json
import logging
import secrets
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from zero.bus.queue import MessageBus
from zero.config import Config
from zero.core.model_switch import session_model, switch_model
from zero.models import MessageBody, MessageIn, StopRequest
from zero.scheduler.store import SchedulerStore
from zero.session.store import SESSIONS_ROOT, SessionStore

logger = logging.getLogger(__name__)

CHANNEL = "web"
BINDING_PATH = SESSIONS_ROOT / CHANNEL / ".web_current.json"


class WebChannel:
    """Serve one local WebSocket endpoint and bridge it to the message bus."""

    def __init__(self, msg_bus: MessageBus, config: Config | None = None):
        self.msg_bus = msg_bus
        self.config = config
        self.web_config = config.channels.web if config is not None else None

        self._out_queue = msg_bus.subscribe_out(CHANNEL)
        self._stream_queue = msg_bus.subscribe_stream(CHANNEL)
        self._clients: set[ServerConnection] = set()

        # 一次性 token：本机其它网页无法凭 ws://127.0.0.1 猜中即可连接
        self.token = secrets.token_urlsafe(24)
        self._host = self.web_config.host if self.web_config else "127.0.0.1"
        self._port = self.web_config.port if self.web_config else 0

        self.stop_event = asyncio.Event()
        self._ready = asyncio.Event()
        self._workers: list[asyncio.Task] = []
        self._loop: asyncio.AbstractEventLoop | None = None

        self._source_id = ""
        self.session_id = ""
        self._load_binding()

    # ------------------------------------------------------------------
    # Bootstrap / lifecycle
    # ------------------------------------------------------------------

    @property
    def ws_url(self) -> str:
        """Client-facing WebSocket URL (wildcard binds are shown as localhost)."""
        host = self._host
        if host in ("", "0.0.0.0", "::"):
            host = "127.0.0.1"
        return f"ws://{host}:{self._port}/ws"

    def bootstrap(self) -> dict[str, Any]:
        """Called by the pywebview js_api from a GUI thread; must not touch the loop."""
        return {
            "ready": self._ready.is_set(),
            "wsUrl": self.ws_url if self._port else "",
            "token": self.token,
        }

    async def wait_ready(self, timeout: float = 10.0) -> None:
        await asyncio.wait_for(self._ready.wait(), timeout)

    async def serve(self) -> None:
        """Run the server and bus consumers until stop_event is set."""
        self._loop = asyncio.get_running_loop()
        self._workers = [
            asyncio.create_task(self._out_loop()),
            asyncio.create_task(self._stream_loop()),
        ]
        try:
            async with serve(
                self._handle,
                self._host,
                self._port,
                max_size=8 * 1024 * 1024,
            ) as server:
                sock = server.sockets[0]
                self._port = sock.getsockname()[1]
                self._ready.set()
                logger.info("web channel listening on %s", self.ws_url)
                await self.stop_event.wait()
        finally:
            for task in self._workers:
                task.cancel()
            self._workers.clear()
            if self._clients:
                for ws in list(self._clients):
                    try:
                        await ws.close(code=1001, reason="server shutdown")
                    except Exception:
                        pass
                self._clients.clear()

    def stop(self) -> None:
        """Stop the server. Safe to call from the GUI thread."""
        if self._loop is None or self._loop.is_closed():
            self.stop_event.set()
            return
        try:
            self._loop.call_soon_threadsafe(self.stop_event.set)
        except RuntimeError:  # loop closed between check and call
            pass

    # ------------------------------------------------------------------
    # Session binding (mirrors CLI: one source_id per session)
    # ------------------------------------------------------------------

    def _load_binding(self) -> None:
        if BINDING_PATH.exists():
            try:
                data = json.loads(BINDING_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = None
            if isinstance(data, dict) and data.get("source_id") and data.get("session_id"):
                self._source_id = data["source_id"]
                self.session_id = data["session_id"]
                return
        self._new_session()

    def _save_binding(self) -> None:
        BINDING_PATH.parent.mkdir(parents=True, exist_ok=True)
        BINDING_PATH.write_text(json.dumps({
            "source_id": self._source_id,
            "session_id": self.session_id,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    def _bind(self, source_id: str, session_id: str) -> None:
        self._source_id = source_id
        self.session_id = session_id
        self._save_binding()

    def _new_session(self) -> None:
        source_id = f"web-{uuid4().hex[:8]}"
        session_id = SessionStore.create_session(CHANNEL, source_id)
        self._bind(source_id, session_id)

    # ------------------------------------------------------------------
    # Snapshot helpers
    # ------------------------------------------------------------------

    def _title(self) -> str:
        return SessionStore.get_title(CHANNEL, self.session_id)

    def _model(self) -> str:
        if self.config is None:
            return ""
        try:
            return session_model(self.config, CHANNEL, self.session_id)
        except ValueError:
            return "?"

    def _sessions(self) -> list[dict[str, Any]]:
        result = []
        for entry in SessionStore.list_sessions(CHANNEL):
            if not SessionStore.has_messages(CHANNEL, entry.session_id):
                continue
            title = entry.title or SessionStore.make_title(
                SessionStore.first_user_message(CHANNEL, entry.session_id))
            result.append({
                "id": entry.session_id,
                "sourceId": entry.source_id,
                "title": title,
                "date": entry.date,
                "updatedAt": entry.last_updated_at.strftime("%Y-%m-%d %H:%M"),
            })
        return result

    def _history(self, session_id: str) -> list[dict[str, Any]]:
        messages = []
        for msg in SessionStore.load_history(session_id, CHANNEL):
            if msg.sender not in ("user", "assistant"):
                continue
            content = msg.body.content or ""
            if not content:
                continue
            messages.append({
                "id": msg.id,
                "role": msg.sender,
                "content": content,
                "compact": (msg.metadata or {}).get("kind") == "compact",
            })
        return messages

    def _state(self) -> dict[str, Any]:
        return {
            "channel": CHANNEL,
            "session": {
                "id": self.session_id,
                "sourceId": self._source_id,
                "title": self._title(),
            },
            "sessions": self._sessions(),
            "model": self._model(),
            "models": sorted(self.config.models) if self.config else [],
        }

    def _tasks(self) -> list[dict[str, Any]]:
        tasks = [
            t for t in SchedulerStore.load_all()
            if t.channel == CHANNEL and t.session_id == self.session_id
        ]
        tasks.sort(key=lambda t: (t.trigger_at is None,
                                  t.trigger_at or t.created_at))

        def _dt(value):
            return value.strftime("%Y-%m-%d %H:%M") if value else ""

        return [{
            "id": t.id,
            "status": t.status,
            "scheduleType": t.schedule_type,
            "cron": t.cron,
            "triggerAt": _dt(t.trigger_at),
            "lastRunAt": _dt(t.last_run_at),
            "instruction": t.instruction.removeprefix("[Scheduled Task]"),
        } for t in tasks]

    # ------------------------------------------------------------------
    # WebSocket handling
    # ------------------------------------------------------------------

    def _authorized(self, ws: ServerConnection) -> bool:
        query = parse_qs(urlsplit(ws.request.path).query)
        return secrets.compare_digest(query.get("token", [""])[0], self.token)

    async def _handle(self, ws: ServerConnection) -> None:
        if not self._authorized(ws):
            await ws.close(code=1008, reason="invalid token")
            return
        self._clients.add(ws)
        try:
            await self._send(ws, {"type": "state", **self._state()})
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    await self._send(ws, {
                        "type": "notice", "level": "error",
                        "text": "无法解析的消息",
                    })
                    continue
                if not isinstance(data, dict):
                    continue
                try:
                    await self._dispatch(ws, data)
                except Exception:
                    logger.exception("web channel message failed")
                    await self._send(ws, {
                        "type": "notice", "level": "error",
                        "text": "后端处理消息失败，详见 debug.log",
                    })
        except ConnectionClosed:
            pass
        finally:
            self._clients.discard(ws)

    async def _send(self, ws: ServerConnection, payload: dict[str, Any]) -> None:
        try:
            await ws.send(json.dumps(payload, ensure_ascii=False))
        except ConnectionClosed:
            self._clients.discard(ws)

    async def _broadcast(self, payload: dict[str, Any]) -> None:
        text = json.dumps(payload, ensure_ascii=False)
        for ws in list(self._clients):
            try:
                await ws.send(text)
            except ConnectionClosed:
                self._clients.discard(ws)

    # ------------------------------------------------------------------
    # Client commands
    # ------------------------------------------------------------------

    async def _dispatch(self, ws: ServerConnection, data: dict[str, Any]) -> None:
        kind = data.get("type")
        if kind == "send":
            await self._on_send(ws, data)
        elif kind == "stop":
            await self._on_stop(data)
        elif kind == "approval":
            req_id = str(data.get("reqId") or "")
            answer = str(data.get("answer") or "")
            ok = self.msg_bus.approvals.resolve(req_id, answer)
            if not ok:
                await self._send(ws, {
                    "type": "notice", "level": "error",
                    "text": "审批已超时或不存在",
                })
        elif kind == "sessions":
            await self._send(ws, {"type": "sessions", "sessions": self._sessions()})
        elif kind == "history":
            session_id = str(data.get("sessionId") or self.session_id)
            await self._send(ws, {
                "type": "history",
                "sessionId": session_id,
                "messages": self._history(session_id),
            })
        elif kind == "select":
            await self._on_select(ws, str(data.get("sessionId") or ""))
        elif kind == "new":
            self._new_session()
            await self._broadcast({"type": "state", **self._state()})
            await self._send(ws, {
                "type": "history", "sessionId": self.session_id, "messages": [],
            })
        elif kind == "title":
            title = str(data.get("title") or "").strip()
            SessionStore.set_title(CHANNEL, self.session_id, title)
            await self._broadcast({"type": "state", **self._state()})
        elif kind == "model":
            await self._on_model(ws, str(data.get("model") or "").strip())
        elif kind == "tasks":
            await self._send(ws, {"type": "tasks", "tasks": self._tasks()})
        else:
            logger.warning("unknown web message type: %r", kind)

    async def _on_send(self, ws: ServerConnection, data: dict[str, Any]) -> None:
        text = str(data.get("text") or "").strip()
        if not text:
            return
        msg = MessageIn(
            channel=CHANNEL,
            session_id=self.session_id,
            body=MessageBody(content=text),
            channel_message_id=str(uuid4()),
            channel_sender_id=self._source_id,
            channel_session_id=self._source_id,
        )
        await self.msg_bus.in_queue.enqueue(msg)
        await self._send(ws, {
            "type": "queued",
            "turnId": msg.id,
            "sessionId": self.session_id,
        })

    async def _on_stop(self, data: dict[str, Any]) -> None:
        turn_id = str(data.get("turnId") or "")
        if not turn_id:
            return
        await self.msg_bus.stop_queue.enqueue(StopRequest(
            channel=CHANNEL, session_id=self.session_id, turn_id=turn_id))

    async def _on_select(self, ws: ServerConnection, session_id: str) -> None:
        entry = next(
            (s for s in SessionStore.list_sessions(CHANNEL)
             if s.session_id == session_id),
            None,
        )
        if entry is None:
            await self._send(ws, {
                "type": "notice", "level": "error", "text": "会话不存在",
            })
            return
        self._bind(entry.source_id, entry.session_id)
        await self._broadcast({"type": "state", **self._state()})
        await self._send(ws, {
            "type": "history",
            "sessionId": session_id,
            "messages": self._history(session_id),
        })

    async def _on_model(self, ws: ServerConnection, model: str) -> None:
        if self.config is None:
            await self._send(ws, {
                "type": "notice", "level": "error", "text": "未注入配置",
            })
            return
        if not model:
            await self._send(ws, {
                "type": "models",
                "models": sorted(self.config.models),
                "current": self._model(),
            })
            return
        try:
            result = await switch_model(
                self.config, CHANNEL, self.session_id, model)
        except ValueError as exc:
            await self._send(ws, {
                "type": "notice", "level": "error", "text": str(exc),
            })
            return
        except Exception:
            logger.exception("web model switch failed")
            await self._send(ws, {
                "type": "notice", "level": "error",
                "text": "模型切换失败，详见 debug.log",
            })
            return
        if result.switched:
            note = "（已按新窗口压缩会话）" if result.compacted else ""
            await self._broadcast({
                "type": "notice", "level": "info",
                "text": f"模型已切换 {result.old_model} → {result.new_model}{note}",
            })
        await self._broadcast({"type": "state", **self._state()})

    # ------------------------------------------------------------------
    # Bus consumers
    # ------------------------------------------------------------------

    async def _out_loop(self) -> None:
        while True:
            msg_out = await self._out_queue.dequeue()
            await self._broadcast({
                "type": "out",
                "message": msg_out.model_dump(mode="json", by_alias=True),
            })
            # 会话标题在首条用户消息后由 core 自动生成，回合结束时刷新列表
            await self._broadcast({"type": "sessions", "sessions": self._sessions()})

    async def _stream_loop(self) -> None:
        while True:
            event = await self._stream_queue.dequeue()
            await self._broadcast({
                "type": "stream",
                "event": event.model_dump(mode="json", by_alias=True),
            })
