"""
Feishu (Lark) channel module — WebSocket long connection mode.

Based on the official lark-oapi SDK, establishes a persistent WebSocket
connection with the Feishu Open Platform to receive events, and uses
the OpenAPI client to send messages back.

Official docs:
  - Long connection: https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case
  - Send message:    https://open.feishu.cn/document/server-side-sdk/python--sdk/invoke-server-api
"""

import json
import asyncio
import logging
import threading
from typing import Any

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    CreateMessageResponse,
)

from zero.bus.queue import BusQueueFull, MessageBus, MessageOutQueue
from zero.models import (
    CHANNEL_FEISHU, FeishuConfig, MessageBody, MessageIn, MessageOut,
    StreamEvent)
from zero.session.store import SessionStore

logger = logging.getLogger(__name__)
CHANNEL = CHANNEL_FEISHU


class FeishuChannel:
    """
    Feishu channel with WebSocket long connection.

    1. Receives messages: lark.ws.Client (WebSocket) → event handler → MessageIn → MessageBus
    2. Sends messages:    MessageBus → MessageOut → lark.Client (OpenAPI) → Feishu

    Usage:
        channel = FeishuChannel(config, msg_bus)
        await channel.start()          # starts WS receiver + bus sender concurrently
        await channel.stop()           # graceful shutdown

    Or run independently:
        channel = FeishuChannel(config)
        await channel.connect()        # only WebSocket receiver, no bus integration
        channel.send_text("oc_xxx", "hello")  # direct send
    """

    name = CHANNEL

    def __init__(
        self,
        config: FeishuConfig,
        session_store: SessionStore,
        msg_bus: MessageBus | None = None,
    ):
        self.config = config
        # 飞书 WS 线程也要解析 session，存储实例由组合根显式传入
        self.session_store = session_store
        self.msg_bus = msg_bus
        # 向 bus 认领 feishu 专属出站队列，dispatcher 只会路由 channel=feishu 的消息
        self._out_queue: MessageOutQueue | None = (
            msg_bus.subscribe_out(CHANNEL) if msg_bus else None
        )

        # lark API client (for sending messages)
        self._lark_client: lark.Client | None = None
        # lark WebSocket client (for receiving events)
        self._ws_client: lark.ws.Client | None = None
        # event handler dispatcher
        self._event_handler = None

        # asyncio loop reference (for cross-thread scheduling)
        self._loop: asyncio.AbstractEventLoop | None = None
        # WebSocket thread
        self._ws_thread: threading.Thread | None = None
        self._running = False

    # ------------------------------------------------------------------
    # Lazy initialization
    # ------------------------------------------------------------------

    @property
    def lark_client(self) -> lark.Client:
        """Lazily build the lark API client for sending messages."""
        if self._lark_client is None:
            self._lark_client = (
                lark.Client.builder()
                .app_id(self.config.app_id)
                .app_secret(self.config.app_secret)
                .log_level(lark.LogLevel.INFO)
                .build()
            )
        return self._lark_client

    def _build_event_handler(self) -> lark.EventDispatcherHandler:
        """Build event dispatcher with registered handlers."""
        return (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message_receive)
            .build()
        )

    # ------------------------------------------------------------------
    # Event handlers (called by lark SDK in WS thread)
    # ------------------------------------------------------------------

    def _on_message_receive(self, data: lark.im.v1.P2ImMessageReceiveV1) -> None:
        """
        Handle im.message.receive_v1 event.

        Called synchronously in the WebSocket thread. Converts the event
        to a MessageIn and pushes it to the asyncio MessageBus.
        """
        try:
            event = data.event
            msg = event.message
            sender = event.sender

            # Extract text content
            content_str = msg.content or "{}"
            text = ""
            if msg.message_type == "text":
                try:
                    text = json.loads(content_str).get("text", "")
                except (json.JSONDecodeError, TypeError):
                    text = content_str
            else:
                text = content_str

            chat_id = msg.chat_id or ""
            message_id = msg.message_id or ""
            sender_id = sender.sender_id.open_id if sender and sender.sender_id else ""

            # 只记路由元信息：正文长期留存既无排障价值，又会把用户消息抄进日志
            logger.debug(
                "Feishu message received: chat_id=%s, message_id=%s, type=%s",
                chat_id, message_id, msg.message_type,
            )

            msg_in = MessageIn(
                session_id=self.session_store.resolve_session(
                    CHANNEL, chat_id),
                channel=CHANNEL,
                body=MessageBody(content=text),
                channel_message_id=message_id,
                channel_sender_id=sender_id,
                channel_session_id=chat_id,
            )

            # Push to bus from WS thread → asyncio loop
            if self.msg_bus and self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._submit(msg_in, chat_id), self._loop)
            else:
                logger.warning("MessageBus or event loop not available, message dropped")

        except Exception:
            logger.exception("Error handling Feishu message event")

    async def _submit(self, msg_in: MessageIn, chat_id: str) -> None:
        """把入站消息投递到总线；队列满时直接在该会话里回执。

        在事件循环线程执行（WS 回调用 run_coroutine_threadsafe 调度）。
        拒绝必须让用户看见，否则飞书侧只会表现为「发了没反应」。
        """
        try:
            self.msg_bus.in_queue.enqueue_nowait(msg_in)
        except BusQueueFull:
            logger.warning("Feishu 入站队列已满，拒绝 chat_id=%s", chat_id)
            await self.send_text(chat_id, "系统繁忙：消息队列已满，请稍后重试")

    # ------------------------------------------------------------------
    # Start / Stop
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """
        Start the channel: WebSocket receiver + bus sender concurrently.

        - WebSocket runs in a background thread (lark.ws.Client.start() is blocking)
        - Bus sender runs as an asyncio task consuming MessageOut from the bus
        """
        self._loop = asyncio.get_running_loop()
        self._running = True

        # Start WebSocket receiver in background thread
        self._start_ws_thread()

        # If bus integration is configured, start the sender task
        if self.msg_bus:
            await self._bus_sender_loop()

    async def connect(self) -> None:
        """
        Start only the WebSocket receiver (no bus sender loop).

        Useful when you want to handle events manually or test the connection.
        Blocks the asyncio loop until stopped.
        """
        self._loop = asyncio.get_running_loop()
        self._running = True
        self._start_ws_thread()

        # Keep the coroutine alive
        while self._running:
            await asyncio.sleep(1)

    def _start_ws_thread(self) -> None:
        """Start the WebSocket client in a daemon thread."""
        self._event_handler = self._build_event_handler()
        self._ws_client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            event_handler=self._event_handler,
            log_level=lark.LogLevel.INFO,
        )

        def _ws_run():
            try:
                logger.info("Starting Feishu WebSocket connection...")
                self._ws_client.start()
            except Exception:
                logger.exception("Feishu WebSocket connection error")

        self._ws_thread = threading.Thread(target=_ws_run, daemon=True, name="feishu-ws")
        self._ws_thread.start()

    async def stop(self) -> None:
        """Gracefully stop the channel."""
        self._running = False
        # lark.ws.Client doesn't expose a clean stop API;
        # the daemon thread will be terminated when the process exits
        logger.info("Feishu channel stopped")

    # ------------------------------------------------------------------
    # Bus sender: consume MessageOut → send to Feishu
    # ------------------------------------------------------------------

    async def _bus_sender_loop(self) -> None:
        """Consume MessageOut from the feishu-only queue and send to Feishu."""
        while self._running:
            try:
                msg_out = await self._out_queue.dequeue()
                # 防御：正常情况下 dispatcher 已按 channel 路由，不会走到这里
                if msg_out.channel != CHANNEL:
                    logger.warning(
                        "Feishu channel got MessageOut for channel %r, ignored",
                        msg_out.channel,
                    )
                    continue
                await self.deliver(msg_out)
            except Exception:
                logger.exception("Error sending message to Feishu")

    async def deliver(self, message: MessageOut) -> None:
        """Send one final agent reply back to its originating chat."""
        await self.send_response(message)

    async def deliver_stream(self, event: StreamEvent) -> None:
        """Feishu renders only final replies; incremental events are unused."""
        return

    # ------------------------------------------------------------------
    # Send messages (async wrappers around sync lark SDK)
    # ------------------------------------------------------------------

    async def send_response(
        self,
        message_out: MessageOut,
        receive_id: str | None = None,
        receive_id_type: str = "chat_id",
    ) -> dict[str, Any]:
        """
        Send a MessageOut back to Feishu.

        Args:
            message_out: Agent output message
            receive_id: Target chat/user ID (falls back to message_out.channel_session_id)
            receive_id_type: ID type (chat_id, open_id, user_id, etc.)

        Returns:
            API response data
        """
        target_id = receive_id or message_out.channel_session_id
        if not target_id:
            raise ValueError("No receive_id provided and no channel_session_id on message")

        return await self.send_post(target_id, message_out.body.content, receive_id_type)

    async def send_text(
        self,
        receive_id: str,
        text: str,
        receive_id_type: str = "chat_id",
    ) -> dict[str, Any]:
        """Send a text message to a Feishu chat or user."""
        return await asyncio.to_thread(
            self._send_message_sync,
            receive_id=receive_id,
            msg_type="text",
            content=json.dumps({"text": text}),
            receive_id_type=receive_id_type,
        )

    async def send_post(
        self,
        receive_id: str,
        content: str,
        receive_id_type: str = "chat_id",
    ) -> dict[str, Any]:
        """Send a rich text (post) message to a Feishu chat or user."""

        post_content = {
            "zh_cn": {
                "content": [
                    [{"tag": "md", "text": content}]
                ]
            }
        }
        return await asyncio.to_thread(
            self._send_message_sync,
            receive_id=receive_id,
            msg_type="post",
            content=json.dumps(post_content),
            receive_id_type=receive_id_type,
        )

    async def send_card(
        self,
        receive_id: str,
        card: dict[str, Any],
        receive_id_type: str = "chat_id",
    ) -> dict[str, Any]:
        """Send an interactive card to a Feishu chat or user."""
        return await asyncio.to_thread(
            self._send_message_sync,
            receive_id=receive_id,
            msg_type="interactive",
            content=json.dumps(card),
            receive_id_type=receive_id_type,
        )

    # ------------------------------------------------------------------
    # Sync send (internal, called via asyncio.to_thread)
    # ------------------------------------------------------------------

    def _send_message_sync(
        self,
        receive_id: str,
        msg_type: str,
        content: str,
        receive_id_type: str = "chat_id",
    ) -> dict[str, Any]:
        """
        Synchronous message send via lark SDK.

        POST /open-apis/im/v1/messages?receive_id_type={type}
        """
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type(msg_type)
                .content(content)
                .build()
            )
            .build()
        )

        response: CreateMessageResponse = self.lark_client.im.v1.message.create(request)

        if not response.success():
            logger.error(
                "Failed to send Feishu message: code=%s, msg=%s, log_id=%s",
                response.code, response.msg, response.get_log_id(),
            )
            raise RuntimeError(
                f"Failed to send Feishu message: code={response.code}, msg={response.msg}"
            )

        logger.info(
            "Feishu message sent: message_id=%s",
            response.data.message_id if response.data else "unknown",
        )
        return lark.JSON.marshal(response.data) if response.data else {}
