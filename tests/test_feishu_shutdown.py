"""停用路径：必须等 SDK 线程真正退出，超时要明确报错。"""

import asyncio
import logging
import threading
import time

import pytest
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from zero.channels.feishu import channel as channel_module
from zero.channels.feishu.channel import (
    _INTENTIONAL_STOP,
    _ExpectedSdkCloseFilter,
    _handle_sdk_loop_exception,
    FeishuChannel,
    _close_sdk_loop,
)
from zero.channels.feishu.control import FeishuController
from zero.models import FeishuConfig

NORMAL_CLOSE = (
    "receive message loop exit, err: sent 1000 (OK); "
    "then received 1000 (OK)")


def test_lark_close_filter_only_during_intentional_stop():
    filt = _ExpectedSdkCloseFilter()

    def record(message: str, level: int = logging.ERROR) -> logging.LogRecord:
        return logging.LogRecord("Lark", level, __file__, 1, message, (), None)

    assert _INTENTIONAL_STOP.is_set() is False
    # 非主动停止：照常输出
    assert filt.filter(record(NORMAL_CLOSE)) is True

    _INTENTIONAL_STOP.set()
    try:
        assert filt.filter(record(NORMAL_CLOSE)) is False
        # 异常关闭、其它错误、低级别日志都不能吞
        assert filt.filter(record(
            "receive message loop exit, err: no close frame received")) is True
        assert filt.filter(record(
            "connect failed, err: ConnectionRefusedError")) is True
        assert filt.filter(record(NORMAL_CLOSE, logging.WARNING)) is True
    finally:
        _INTENTIONAL_STOP.clear()


def test_loop_handler_only_swallows_normal_close_gc_warning():
    handled = []

    class FakeLoop:
        def default_exception_handler(self, context):
            handled.append(context)

    loop = FakeLoop()
    gc_warning = {"message": "Task exception was never retrieved",
                  "exception": ConnectionClosedOK(None, None)}

    _handle_sdk_loop_exception(loop, gc_warning)
    assert handled == []

    # 异常断开不能被吞
    _handle_sdk_loop_exception(loop, {
        "message": "Task exception was never retrieved",
        "exception": ConnectionClosedError(None, None)})
    assert len(handled) == 1

    # 其它未处理异常照常输出
    _handle_sdk_loop_exception(loop, {
        "message": "Task exception was never retrieved",
        "exception": RuntimeError("boom")})
    assert len(handled) == 2


async def _select():
    """模拟 SDK 的 ``client.start``：run_until_complete 一个永不返回的协程。"""
    await asyncio.sleep(3600)


def test_stop_ends_sdk_thread_and_reports_disabled():
    """只靠 loop.stop 让 SDK 线程自然退出，不 cancel 它的内部任务。"""
    thread_errors = []
    old_hook = threading.excepthook
    threading.excepthook = thread_errors.append

    class FakeClient:
        _auto_reconnect = True

        async def _disconnect(self):
            return None

    sdk_loop = asyncio.new_event_loop()
    started = threading.Event()

    def run():
        asyncio.set_event_loop(sdk_loop)
        started.set()
        try:
            sdk_loop.run_until_complete(_select())
        except (asyncio.CancelledError, RuntimeError):
            # 主动 loop.stop() 的正常收尾；真实 _ws_run 也是这样接住的
            pass
        finally:
            _close_sdk_loop(sdk_loop)

    thread = threading.Thread(target=run, name="feishu-ws", daemon=True)
    thread.start()
    assert started.wait(1)

    states = []
    channel = FeishuChannel(
        FeishuConfig(app_id="cli-1", app_secret="secret"), None,
        on_state=lambda state, error: states.append((state, error)))
    channel._state = "connected"
    channel._ws_client = FakeClient()
    channel._sdk_loop = sdk_loop
    channel._ws_thread = thread
    channel._running = True

    try:
        stopped = asyncio.run(channel.stop())
    finally:
        threading.excepthook = old_hook

    assert stopped is True
    assert not thread.is_alive()
    assert ("disabled", "") in states
    assert thread_errors == []


def test_stop_reports_failure_when_thread_stays_alive(monkeypatch):
    """线程不退时不能报「已关闭」，要保留引用并返回明确错误。"""
    monkeypatch.setattr(channel_module, "STOP_JOIN_TIMEOUT", 0.05)
    thread = threading.Thread(target=time.sleep, args=(0.3,), daemon=True)
    thread.start()

    states = []
    channel = FeishuChannel(
        FeishuConfig(app_id="cli-1", app_secret="secret"), None,
        on_state=lambda state, error: states.append((state, error)))
    channel._ws_client = None
    channel._sdk_loop = None
    channel._ws_thread = thread
    channel._running = True

    stopped = asyncio.run(channel.stop())
    thread.join(1)

    assert stopped is False
    assert states[-1][0] == "failed"
    assert "没有按预期退出" in states[-1][1]
    # 引用必须保留，下一次停用 / 重连才能继续处理
    assert channel._ws_thread is thread


class _StuckChannel:
    def __init__(self):
        self.stop_called = 0

    async def start(self) -> None:
        await asyncio.Event().wait()

    async def stop(self, *, keep_state: bool = False) -> bool:
        self.stop_called += 1
        return False


def test_controller_disable_raises_and_keeps_channel_on_failure():
    class FakeManager:
        def __init__(self):
            self.enabled = []

        def set_feishu_enabled(self, enabled):
            self.enabled.append(enabled)

    manager = FakeManager()
    controller = FeishuController(
        FeishuConfig(app_id="cli-1", app_secret="secret"), None, None, manager)
    stuck = _StuckChannel()
    controller._channel = stuck
    controller._state = "connected"

    with pytest.raises(OSError, match="线程没有退出"):
        asyncio.run(controller.disable())

    assert stuck.stop_called == 1
    # 没有把 enabled=false 写进配置，也没有把界面改成「已关闭」
    assert manager.enabled == []
    assert controller.snapshot()["state"] == "connected"
    # channel 保留下来，下一次停用还能继续等
    assert controller._channel is stuck
