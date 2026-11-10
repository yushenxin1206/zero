"""
Web channel — local WebSocket server for the pywebview desktop UI.

The channel is deliberately transport-only: it translates between the
MessageBus and JSON frames on WebSocket connections. All agent behavior
(turns, tools, approvals, scheduled tasks) stays in the core, and all
session/model/task rules come from ``ChannelService`` — this module only
serializes them into frames.

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

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from zero.bus.queue import MessageBus
from zero.channels.base import BusChannel
from zero.channels.service import ChannelService
from zero.config import Config
from zero.models import MessageOut, StreamEvent
from zero.data_paths import DataPaths
from zero.permission.envelope import PermissionState
from zero.session.store import SessionStore
from zero.scheduler.store import SchedulerStore

logger = logging.getLogger(__name__)

CHANNEL = "web"


class WebChannel(BusChannel):
    """Serve one local WebSocket endpoint and bridge it to the message bus."""

    name = CHANNEL

    def __init__(self, msg_bus: MessageBus, session_store: SessionStore,
                 scheduler_store: SchedulerStore, data_paths: DataPaths,
                 permission: PermissionState,
                 config: Config | None = None):
        super().__init__(msg_bus)
        self.service = ChannelService(
            CHANNEL, msg_bus=msg_bus, session_store=session_store,
            scheduler_store=scheduler_store, data_paths=data_paths,
            permission=permission, config=config)
        self.web_config = config.channels.web if config is not None else None

        self._clients: set[ServerConnection] = set()

        # 一次性 token：本机其它网页无法凭 ws://127.0.0.1 猜中即可连接
        self.token = secrets.token_urlsafe(24)
        self._host = self.web_config.host if self.web_config else "127.0.0.1"
        self._port = self.web_config.port if self.web_config else 0

        self.stop_event = asyncio.Event()
        self._ready = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

        if not self.service.restore_binding():
            self.service.new_session()

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

    async def start(self) -> None:
        """Run the server and bus consumers until stop_event is set."""
        self._loop = asyncio.get_running_loop()
        self.start_consumers()
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
            await self.stop_consumers()
            for ws in list(self._clients):
                try:
                    await ws.close(code=1001, reason="server shutdown")
                except Exception:
                    pass
            self._clients.clear()

    async def stop(self) -> None:
        """Stop the server. Safe to call from the GUI thread."""
        if self._loop is None or self._loop.is_closed():
            self.stop_event.set()
            return
        try:
            self._loop.call_soon_threadsafe(self.stop_event.set)
        except RuntimeError:  # loop closed between check and call
            pass

    # ------------------------------------------------------------------
    # Bus consumers
    # ------------------------------------------------------------------

    async def deliver(self, message: MessageOut) -> None:
        await self._broadcast({
            "type": "out",
            "message": message.model_dump(mode="json", by_alias=True),
        })
        # 会话标题在首条用户消息后由 core 自动生成，回合结束时刷新列表
        await self._broadcast({"type": "sessions", "sessions": await self._sessions()})

    async def deliver_stream(self, event: StreamEvent) -> None:
        await self._broadcast({
            "type": "stream",
            "event": event.model_dump(mode="json", by_alias=True),
        })

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def _sessions_sync(self) -> list[dict[str, Any]]:
        return [{
            "id": entry.session_id,
            "sourceId": entry.source_id,
            "title": entry.title,
            "date": entry.date,
            "updatedAt": entry.last_updated_at.strftime("%Y-%m-%d %H:%M"),
        } for entry in self.service.sessions()]

    def _history_sync(self, session_id: str) -> list[dict[str, Any]]:
        return [{
            "id": msg.id,
            "role": msg.sender,
            "content": msg.body.content or "",
            "compact": msg.compact,
        } for msg in self.service.display_history(session_id)]

    def _state_sync(self) -> dict[str, Any]:
        return {
            "channel": CHANNEL,
            "session": {
                "id": self.service.session_id,
                "sourceId": self.service.source_id,
                "title": self.service.title(),
            },
            "sessions": self._sessions_sync(),
            "model": self.service.current_model(),
            "models": self.service.models(),
        }

    def _tasks_sync(self) -> list[dict[str, Any]]:
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
        } for t in self.service.tasks()]

    # 会话列表/历史/任务快照都要读索引与 JSONL：统一在线程池构建，
    # WebSocket 处理协程在磁盘 IO 期间不阻塞事件循环
    async def _sessions(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._sessions_sync)

    async def _history(self, session_id: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._history_sync, session_id)

    async def _state(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._state_sync)

    async def _tasks(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._tasks_sync)

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
            await self._send(ws, {"type": "state", **await self._state()})
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
            turn_id = str(data.get("turnId") or "")
            if turn_id:
                await self.service.request_stop(turn_id)
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
            await self._send(ws, {
                "type": "sessions", "sessions": await self._sessions()})
        elif kind == "history":
            session_id = str(data.get("sessionId") or self.service.session_id)
            await self._send(ws, {
                "type": "history",
                "sessionId": session_id,
                "messages": await self._history(session_id),
            })
        elif kind == "select":
            await self._on_select(ws, str(data.get("sessionId") or ""))
        elif kind == "new":
            await asyncio.to_thread(self.service.new_session)
            await self._broadcast({"type": "state", **await self._state()})
            await self._send(ws, {
                "type": "history",
                "sessionId": self.service.session_id,
                "messages": [],
            })
        elif kind == "title":
            await asyncio.to_thread(
                self.service.rename, str(data.get("title") or "").strip())
            await self._broadcast({"type": "state", **await self._state()})
        elif kind == "model":
            await self._on_model(ws, str(data.get("model") or "").strip())
        elif kind == "tasks":
            await self._send(ws, {"type": "tasks", "tasks": await self._tasks()})
        else:
            logger.warning("unknown web message type: %r", kind)

    async def _on_send(self, ws: ServerConnection, data: dict[str, Any]) -> None:
        text = str(data.get("text") or "").strip()
        if not text:
            return
        msg = await self.service.submit(text)
        await self._send(ws, {
            "type": "queued",
            "turnId": msg.id,
            "sessionId": self.service.session_id,
        })

    async def _on_select(self, ws: ServerConnection, session_id: str) -> None:
        entry = await asyncio.to_thread(self.service.find, session_id)
        if entry is None:
            await self._send(ws, {
                "type": "notice", "level": "error", "text": "会话不存在",
            })
            return
        await asyncio.to_thread(
            self.service.bind, entry.source_id, entry.session_id)
        await self._broadcast({"type": "state", **await self._state()})
        await self._send(ws, {
            "type": "history",
            "sessionId": session_id,
            "messages": await self._history(session_id),
        })

    async def _on_model(self, ws: ServerConnection, model: str) -> None:
        if self.service.config is None:
            await self._send(ws, {
                "type": "notice", "level": "error", "text": "未注入配置",
            })
            return
        if not model:
            current = await asyncio.to_thread(self.service.current_model)
            await self._send(ws, {
                "type": "models",
                "models": self.service.models(),
                "current": current,
            })
            return
        try:
            result = await self.service.switch_model(model)
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
        await self._broadcast({"type": "state", **await self._state()})
