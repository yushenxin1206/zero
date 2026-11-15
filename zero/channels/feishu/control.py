"""飞书通道的运行期开关。

同一时刻只允许一条长连接：启停都经过同一把锁，重复点击不会拉起第二条连接。
开关会回写 ``channels.feishu.enabled``，但本模块不读回配置——运行期状态以
实际连接为准，避免「配置说开着、连接早断了」的假象。
"""

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from zero.bus.queue import MessageBus
from zero.channels.feishu.channel import (
    STATE_CONNECTED,
    STATE_CONNECTING,
    STATE_DISABLED,
    STATE_FAILED,
    FeishuChannel,
    StateCallback,
)
from zero.channels.feishu.registration import QrConnector
from zero.config import ConfigManager
from zero.models import FeishuConfig
from zero.session.store import SessionStore

logger = logging.getLogger(__name__)

STATE_UNCONFIGURED = "unconfigured"
ChannelFactory = Callable[
    [FeishuConfig, SessionStore, MessageBus | None, StateCallback | None],
    FeishuChannel,
]
ChangeCallback = Callable[[dict[str, Any]], None]
QrEventCallback = Callable[[dict[str, Any]], None]


class FeishuController:
    """管理飞书长连接的启动、停止与状态快照。"""

    def __init__(
        self,
        config: FeishuConfig,
        session_store: SessionStore,
        msg_bus: MessageBus | None = None,
        manager: ConfigManager | None = None,
        channel_factory: ChannelFactory | None = None,
    ):
        self._config = config
        self._session_store = session_store
        self._msg_bus = msg_bus
        self._manager = manager
        self._factory: ChannelFactory = channel_factory or FeishuChannel
        self._channel: FeishuChannel | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._on_change: ChangeCallback | None = None
        self._on_qr: QrEventCallback | None = None
        # 扫码结果已经落盘、但当前进程还没用上：重启才生效
        self._restart_required = False
        # 没有配置写入入口就没有落盘能力，扫码功能随之关闭
        self._qr = (
            QrConnector(manager, self._relay_qr)
            if manager is not None else None)
        self._error = ""
        self._state = (
            STATE_DISABLED if self.configured else STATE_UNCONFIGURED)

    @property
    def configured(self) -> bool:
        return bool(
            self._config.app_id.strip() and self._config.app_secret.strip())

    def bind(
        self,
        on_change: ChangeCallback,
        on_qr: QrEventCallback | None = None,
    ) -> None:
        """挂上状态 / 扫码事件回调；Web 通道用它把状态推给界面。"""
        self._on_change = on_change
        self._on_qr = on_qr

    def snapshot(self) -> dict[str, Any]:
        missing = []
        if not self._config.app_id.strip():
            missing.append("appId")
        if not self._config.app_secret.strip():
            missing.append("appSecret")
        return {
            "configured": self.configured,
            "missing": missing,
            "state": self._state,
            "error": self._error,
            "restartRequired": self._restart_required,
        }

    async def start_initial(self, enabled: bool) -> None:
        """启动时按配置决定是否建连；未配置时只更新状态。"""
        if not self.configured:
            self._set_state(STATE_UNCONFIGURED)
            return
        if enabled:
            await self.enable(persist=False)

    async def enable(self, *, persist: bool = True) -> None:
        async with self._lock:
            if not self.configured:
                self._set_state(
                    STATE_UNCONFIGURED,
                    "请先在「配置」里填写飞书 appId 与 appSecret")
                raise ValueError(
                    "飞书未配置：请先在「配置」里填写 appId 与 appSecret")
            if self._task is not None and not self._task.done():
                return
            if persist:
                await self._persist(True)
            self._set_state(STATE_CONNECTING)
            self._task = asyncio.create_task(self._run())

    async def disable(self, *, persist: bool = True) -> None:
        async with self._lock:
            task, self._task = self._task, None
            channel, self._channel = self._channel, None
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            if channel is not None:
                stopped = await channel.stop()
                if stopped is False:
                    # 线程还在跑：不能报「已关闭」，也不能把 enabled=false
                    # 写进配置，否则重启后状态会和实际不一致。保留 channel
                    # 引用，下一次停用还能继续等它退出。
                    self._channel = channel
                    raise OSError(
                        "停用失败：长连接线程没有退出，请重试或重启应用")
            self._set_state(
                STATE_DISABLED if self.configured else STATE_UNCONFIGURED)
            if persist:
                await self._persist(False)

    async def reconnect(self) -> None:
        """失败后的手动重试：先彻底停掉旧连接，再新建一条。"""
        await self.disable(persist=False)
        await self.enable(persist=False)

    async def start_qr(self) -> None:
        """开始扫码建应用；已有会话会被取消后重新生成。"""
        if self._qr is None:
            raise ValueError("当前进程没有配置写入入口，扫码连接不可用")
        await self._qr.start()

    async def cancel_qr(self) -> None:
        if self._qr is not None:
            await self._qr.cancel()

    async def shutdown(self) -> None:
        """进程退出前的兜底：不落盘，也不把异常抛给关闭流程。"""
        try:
            await self.cancel_qr()
            await self.disable(persist=False)
        except Exception:
            logger.exception("关闭飞书通道失败")

    async def _run(self) -> None:
        task = asyncio.current_task()
        channel = self._factory(
            self._config, self._session_store, self._msg_bus,
            self._on_channel_state)
        self._channel = channel
        stop_needed = True
        try:
            await channel.start()
        except asyncio.CancelledError:
            # 取消由 disable() 统一收尾，避免两处同时等同一个 WS 线程
            stop_needed = False
            raise
        except Exception as exc:
            logger.exception("飞书通道异常退出")
            self._set_state(STATE_FAILED, f"连接失败：{exc}")
        finally:
            if stop_needed:
                # 连接自行失败退出时也要把 WS 线程与队列收干净，但保留
                # 失败的对外状态，别让收尾把红点洗成「已关闭」
                with suppress(Exception):
                    await channel.stop(keep_state=True)
            if self._channel is channel:
                self._channel = None
            if self._task is task:
                self._task = None

    def _on_channel_state(self, state: str, error: str) -> None:
        """把通道状态映射成对外状态；可能从 WS 线程调用。

        connected 也透传 error：发送消息遇到权限未批时的提示，要在保持
        「已连接」的前提下让用户看到原因。
        """
        if state == STATE_CONNECTED:
            self._set_state(STATE_CONNECTED, error)
        elif state == STATE_CONNECTING:
            self._set_state(STATE_CONNECTING)
        elif state == STATE_FAILED:
            self._set_state(STATE_FAILED, error)
        else:
            self._set_state(STATE_DISABLED, error)

    def _relay_qr(self, payload: dict[str, Any]) -> None:
        """扫码事件先更新快照状态，再原样转给 Web 通道。"""
        if payload.get("phase") == "success":
            # 凭据已经落盘，但当前进程仍用启动时的配置
            self._restart_required = True
            self._notify()
        if self._on_qr is not None:
            self._on_qr(payload)

    def _set_state(self, state: str, error: str = "") -> None:
        if self._state == state and self._error == error:
            return
        self._state = state
        self._error = error
        self._notify()

    def _notify(self) -> None:
        if self._on_change is not None:
            self._on_change(self.snapshot())

    async def _persist(self, enabled: bool) -> None:
        if self._manager is not None:
            await asyncio.to_thread(
                self._manager.set_feishu_enabled, enabled)
