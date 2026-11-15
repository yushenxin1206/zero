"""主动停用飞书时不产生「正常关闭」错误日志，意外断开仍然可见。"""

import asyncio
import gc
import logging
import threading

from websockets.exceptions import ConnectionClosedOK

from zero.channels.feishu.channel import (
    _INTENTIONAL_STOP,
    FeishuChannel,
    _ExpectedSdkCloseFilter,
)
from zero.models import FeishuConfig

NORMAL_CLOSE = (
    "receive message loop exit, err: sent 1000 (OK); "
    "then received 1000 (OK)")


def _record(message: str, level: int = logging.ERROR) -> logging.LogRecord:
    return logging.LogRecord("Lark", level, __file__, 1, message, (), None)


def test_close_error_filtered_only_while_stopping():
    filt = _ExpectedSdkCloseFilter()
    assert _INTENTIONAL_STOP.is_set() is False

    # 非主动停止：SDK 的报错原样保留
    assert filt.filter(_record(NORMAL_CLOSE)) is True

    _INTENTIONAL_STOP.set()
    try:
        # 主动停用 + 正常关闭码：这正是用户看到的那条噪声
        assert filt.filter(_record(NORMAL_CLOSE)) is False
        # 异常关闭、其它错误、低级别日志都不能吞
        assert filt.filter(_record(
            "receive message loop exit, err: no close frame received")) is True
        assert filt.filter(_record(
            "connect failed, err: ConnectionRefusedError")) is True
        assert filt.filter(_record(NORMAL_CLOSE, logging.WARNING)) is True
    finally:
        _INTENTIONAL_STOP.clear()


def test_stop_drains_finished_receive_task():
    """收包任务已带异常结束时，stop 要把异常取走，不能留给 asyncio 报未处理。"""
    contexts: list = []

    async def main():
        sdk_loop = asyncio.new_event_loop()
        sdk_loop.set_exception_handler(
            lambda loop, ctx: contexts.append(ctx))
        thread = threading.Thread(
            target=sdk_loop.run_forever, daemon=True)
        thread.start()

        class FakeClient:
            _auto_reconnect = True

            async def _disconnect(self):
                # 真实 SDK 的收包任务在连接建立时就已经在跑，这里先等它
                # 在断开过程中以异常退出
                await asyncio.sleep(0.05)

        channel = FeishuChannel(
            FeishuConfig(app_id="cli-1", app_secret="secret"), None)
        channel._ws_client = FakeClient()
        channel._sdk_loop = sdk_loop
        channel._ws_thread = thread
        channel._running = True

        async def receive():
            await asyncio.sleep(0.02)
            raise ConnectionClosedOK(None, None)

        started = threading.Event()

        def start_receive():
            sdk_loop.create_task(receive())
            started.set()

        sdk_loop.call_soon_threadsafe(start_receive)
        assert await asyncio.to_thread(started.wait, 1)

        await channel.stop()

        assert _INTENTIONAL_STOP.is_set() is False
        assert not thread.is_alive()
        sdk_loop.close()

    asyncio.run(main())
    gc.collect()
    assert contexts == []
