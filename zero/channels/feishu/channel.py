"""
Feishu (Lark) channel module — WebSocket long connection mode.

Based on the official lark-oapi SDK, establishes a persistent WebSocket
connection with the Feishu Open Platform to receive events, and uses
the OpenAPI client to send messages back.

Official docs:
  - Long connection: https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case
  - Send message:    https://open.feishu.cn/document/server-side-sdk/python--sdk/invoke-server-api
"""

import asyncio
import json
import logging
import threading
from collections.abc import Callable
from contextlib import suppress
from typing import Any

import lark_oapi as lark
import lark_oapi.ws.client as ws_client_module
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

STATE_CONNECTING = "connecting"
STATE_CONNECTED = "connected"
STATE_DISABLED = "disabled"
StateCallback = Callable[[str, str], None]


def _close_sdk_loop(loop: asyncio.AbstractEventLoop) -> None:
    """停掉连接专用 loop 上残留的收包 / ping 任务并关闭 loop。

    由 WS 线程在 ``client.start()`` 返回后调用，必须与 loop 同线程执行。
    """
    try:
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True))
    except Exception:
        logger.debug("Feishu SDK loop cleanup failed", exc_info=True)
    finally:
        loop.close()


class FeishuChannel:
    """
    Feishu channel with WebSocket long connection.

    1. Receives messages: lark.ws.Client (WebSocket) → event handler → MessageIn → MessageBus
    2. Sends messages:    MessageBus → MessageOut → lark.Client (OpenAPI) → Feishu

    Usage:
        channel = FeishuChannel(config, msg_bus)
        await channel.start()   # 建立长连接并消费出站队列，可被取消
        await channel.stop()    # 真正断开长连接并清理后台任务
    """

    name = CHANNEL

    def __init__(
        self,
        config: FeishuConfig,
        session_store: SessionStore,
        msg_bus: MessageBus | None = None,
        on_state: StateCallback | None = None,
    ):
        self.config = config
        # 飞书 WS 线程也要解析 session，存储实例由组合根显式传入
        self.session_store = session_store
        self.msg_bus = msg_bus
        self._on_state = on_state
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
        # WebSocket thread 与它专用的 SDK loop
        self._ws_thread: threading.Thread | None = None
        self._sdk_loop: asyncio.AbstractEventLoop | None = None
        self._watch_task: asyncio.Task | None = None
        self._running = False
        self._state = STATE_DISABLED
        self._error = ""

    @property
    def state(self) -> str:
        return self._state

    @property
    def error(self) -> str:
        return self._error

    def _set_state(self, state: str, error: str = "") -> None:
        """记录连接状态并通知观察者；可能从 WS 线程调用。"""
        if self._state == state and self._error == error:
            return
        self._state = state
        self._error = error
        if self._on_state is not None:
            self._on_state(state, error)

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
        if not self._running:
            # 停用后连接关闭前仍可能有在途事件，直接丢弃
            return
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
        """建立长连接并消费出站队列；取消本协程只会停发送循环。

        WS 接收仍在独立线程里跑（SDK 的 ``start()`` 是阻塞的），但连接的
        状态与停止都由本类管理，``stop()`` 会真正断开而不是等进程退出。
        """
        if self._running:
            return
        self._loop = asyncio.get_running_loop()
        self._running = True
        self._set_state(STATE_CONNECTING)
        self._start_ws_thread()
        self._watch_task = asyncio.create_task(self._watch_connection())
        try:
            if self.msg_bus:
                await self._bus_sender_loop()
        finally:
            self._running = False

    async def _watch_connection(self) -> None:
        """等 WS 线程完成握手；线程提前退出说明连接失败，结束发送循环。"""
        while self._running:
            client = self._ws_client
            if client is not None and client._conn is not None:
                self._set_state(STATE_CONNECTED)
                return
            thread = self._ws_thread
            if thread is not None and not thread.is_alive():
                self._running = False
                return
            await asyncio.sleep(0.2)

    def _start_ws_thread(self) -> None:
        """在独立线程跑 SDK 的阻塞式长连接。

        SDK 没有可等待的 stop 接口，且用模块级 ``loop`` 调度收包与 ping。
        这里为每条连接准备专用 loop，停用时先断开再停 loop，避免下一条
        连接与残留任务互相干扰。
        """
        sdk_loop = asyncio.new_event_loop()
        ws_client_module.loop = sdk_loop
        self._sdk_loop = sdk_loop
        self._event_handler = self._build_event_handler()
        client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            event_handler=self._event_handler,
            log_level=lark.LogLevel.INFO,
        )
        client.on_reconnecting = lambda: self._set_state(STATE_CONNECTING)
        client.on_reconnected = lambda: self._set_state(STATE_CONNECTED)
        self._ws_client = client

        def _ws_run() -> None:
            try:
                logger.info("Starting Feishu WebSocket connection...")
                client.start()
            except Exception as exc:
                if self._running:
                    logger.exception("Feishu WebSocket connection error")
                    self._set_state(STATE_DISABLED, f"连接失败：{exc}")
            finally:
                _close_sdk_loop(sdk_loop)

        self._ws_thread = threading.Thread(
            target=_ws_run, daemon=True, name="feishu-ws")
        self._ws_thread.start()

    async def stop(self) -> None:
        """真正断开长连接：先关 WS，再停 SDK loop 与发送循环。"""
        self._running = False
        watch, self._watch_task = self._watch_task, None
        if watch is not None:
            watch.cancel()
            with suppress(asyncio.CancelledError):
                await watch

        client, loop, thread = self._ws_client, self._sdk_loop, self._ws_thread
        self._ws_client = None
        self._sdk_loop = None
        self._ws_thread = None
        if client is not None and loop is not None and loop.is_running():
            # SDK 的自动重连是无限循环，必须先关掉再断开，否则刚停就重连
            client._auto_reconnect = False
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    client._disconnect(), loop)
                await asyncio.wait_for(asyncio.wrap_future(fut), 3)
            except Exception:
                logger.warning(
                    "Feishu 长连接断开超时，强制停止", exc_info=True)
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread.is_alive():
            await asyncio.to_thread(thread.join, 3)
        if self._out_queue is not None:
            self._out_queue.clear()
        self._set_state(STATE_DISABLED)
        logger.info("Feishu channel stopped")

    # ------------------------------------------------------------------
    # Bus sender: consume MessageOut → send to Feishu
    # ------------------------------------------------------------------

    async def _bus_sender_loop(self) -> None:
        """Consume MessageOut from the feishu-only queue and send to Feishu."""
        while self._running:
            try:
                # 带上限的等待：连接在后台线程里断掉时，这里最多 0.5s 就能退出，
                # 不必等下一次消息把死连接唤醒
                msg_out = await asyncio.wait_for(
                    self._out_queue.dequeue(), 0.5)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            try:
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
