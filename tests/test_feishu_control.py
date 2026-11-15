"""飞书通道运行期开关：状态机、真实断开与 Web 协议。"""

import asyncio
import json
import threading

import pytest

from zero.channels.feishu.channel import FeishuChannel
from zero.channels.feishu.control import FeishuController
from zero.models import FeishuConfig


class FakeChannel:
    def __init__(self, on_state):
        self.on_state = on_state
        self.release = asyncio.Event()
        self.stop_calls = 0

    async def start(self) -> None:
        self.on_state("connected", "")
        await self.release.wait()

    async def stop(self) -> None:
        self.stop_calls += 1
        self.release.set()


class FakeManager:
    def __init__(self):
        self.calls = []

    def set_feishu_enabled(self, enabled: bool) -> None:
        self.calls.append(enabled)


def _controller(app_id: str = "cli-1", app_secret: str = "secret"):
    manager = FakeManager()
    created: list[FakeChannel] = []

    def factory(config, store, bus, on_state):
        channel = FakeChannel(on_state)
        created.append(channel)
        return channel

    controller = FeishuController(
        FeishuConfig(app_id=app_id, app_secret=app_secret),
        None, None, manager, channel_factory=factory,
    )
    return controller, manager, created


def test_unconfigured_cannot_enable():
    async def main():
        controller, manager, created = _controller("", "")
        with pytest.raises(ValueError):
            await controller.enable()
        assert controller.snapshot()["state"] == "unconfigured"
        assert manager.calls == []
        assert created == []

    asyncio.run(main())


def test_enable_connects_and_disable_really_stops():
    async def main():
        controller, manager, created = _controller()
        assert controller.snapshot()["state"] == "disabled"

        await controller.enable()
        await asyncio.sleep(0)
        assert manager.calls == [True]
        assert controller.snapshot()["state"] == "connected"
        assert len(created) == 1

        await controller.disable()
        assert manager.calls == [True, False]
        assert controller.snapshot()["state"] == "disabled"
        assert created[0].stop_calls == 1
        assert controller._task is None

    asyncio.run(main())


def test_snapshot_reports_missing_credentials():
    controller, _, _ = _controller("", "secret")
    snapshot = controller.snapshot()
    assert snapshot["state"] == "unconfigured"
    assert snapshot["missing"] == ["appId"]


def test_channel_failure_is_reported_as_failed():
    async def main():
        controller, _, created = _controller()
        await controller.enable()
        await asyncio.sleep(0)

        created[0].on_state("failed", "连接失败：凭据无效")

        snapshot = controller.snapshot()
        assert snapshot["state"] == "failed"
        assert "凭据无效" in snapshot["error"]
        await controller.disable()

    asyncio.run(main())


def test_failed_state_survives_cleanup():
    """失败后的收尾不能把状态改回「已关闭」。"""

    class FailingChannel:
        def __init__(self, on_state):
            self.on_state = on_state
            self.cleanup_keep_state = None

        async def start(self) -> None:
            self.on_state("failed", "连接失败：boom")

        async def stop(self, *, keep_state: bool = False) -> None:
            self.cleanup_keep_state = keep_state

    created = []

    def factory(config, store, bus, on_state):
        channel = FailingChannel(on_state)
        created.append(channel)
        return channel

    async def main():
        controller = FeishuController(
            FeishuConfig(app_id="cli-1", app_secret="secret"),
            None, None, channel_factory=factory)
        await controller.enable(persist=False)
        await asyncio.sleep(0.01)

        snapshot = controller.snapshot()
        assert snapshot["state"] == "failed"
        assert "boom" in snapshot["error"]
        assert created[0].cleanup_keep_state is True

    asyncio.run(main())


def test_reconnect_replaces_the_old_channel():
    async def main():
        controller, _, created = _controller()
        await controller.enable()
        await asyncio.sleep(0)

        await controller.reconnect()
        await asyncio.sleep(0)

        assert len(created) == 2
        assert created[0].stop_calls == 1
        assert controller.snapshot()["state"] == "connected"
        await controller.disable()

    asyncio.run(main())


def test_grace_period_turns_connecting_into_failed():
    """短暂重连留在「连接中」，超过宽限期才标红。"""

    async def main():
        states = []
        channel = FeishuChannel(
            FeishuConfig(app_id="cli-1", app_secret="secret"), None,
            on_state=lambda state, error: states.append(state),
            grace_seconds=0.01)
        channel._loop = asyncio.get_running_loop()
        channel._running = True
        channel._set_state("connecting")
        channel._arm_grace_timer()
        await asyncio.sleep(0.05)

        assert channel.state == "failed"
        assert states[-1] == "failed"
        assert "超时" in channel.error

    asyncio.run(main())


def test_repeated_enable_keeps_single_connection():
    async def main():
        controller, manager, created = _controller()
        await controller.enable()
        await asyncio.sleep(0)
        await controller.enable()
        await asyncio.sleep(0)
        assert len(created) == 1
        assert manager.calls == [True]
        await controller.disable()

    asyncio.run(main())


def test_start_initial_respects_config():
    async def main():
        controller, manager, created = _controller()
        await controller.start_initial(False)
        assert controller.snapshot()["state"] == "disabled"
        assert created == []

        await controller.start_initial(True)
        await asyncio.sleep(0)
        assert len(created) == 1
        # 启动时的自动建连不应重复回写开关
        assert manager.calls == []
        await controller.shutdown()

    asyncio.run(main())


def test_lark_ws_client_exposes_stop_primitives():
    """停用依赖 SDK 的几个内部入口；升级 lark-oapi 时让它尽早炸出来。"""
    import inspect

    from lark_oapi.ws.client import Client

    assert hasattr(Client, "_disconnect")
    source = inspect.getsource(Client.__init__)
    for name in ("_auto_reconnect", "on_reconnecting", "on_reconnected", "_conn"):
        assert name in source


class FakeSDKClient:
    def __init__(self):
        self._auto_reconnect = True
        self.disconnected = False

    async def _disconnect(self) -> None:
        self.disconnected = True


def test_channel_stop_disconnects_sdk_and_stops_loop():
    """停用必须真正断开长连接，而不是等进程退出。"""

    async def main():
        channel = FeishuChannel(
            FeishuConfig(app_id="cli-1", app_secret="secret"), None)
        sdk_loop = asyncio.new_event_loop()
        thread = threading.Thread(target=sdk_loop.run_forever, daemon=True)
        thread.start()
        client = FakeSDKClient()
        channel._ws_client = client
        channel._sdk_loop = sdk_loop

        await channel.stop()

        thread.join(2)
        assert client._auto_reconnect is False
        assert client.disconnected is True
        assert channel.state == "disabled"
        assert channel._ws_client is None
        assert channel._sdk_loop is None
        assert not thread.is_alive()

    asyncio.run(main())


class FakeController:
    def __init__(self, configured: bool = True):
        self.configured_value = configured
        self.state = "disabled"
        self.error = ""
        self.calls = []

    def bind(self, on_change) -> None:
        self._on_change = on_change

    def snapshot(self):
        return {
            "configured": self.configured_value,
            "state": self.state,
            "error": self.error,
        }

    async def enable(self) -> None:
        if not self.configured_value:
            self.state = "unconfigured"
            self.error = "请先在配置里填写 appId"
            raise ValueError(self.error)
        self.calls.append("enable")
        self.state = "connecting"

    async def disable(self) -> None:
        self.calls.append("disable")
        self.state = "disabled"

    async def reconnect(self) -> None:
        self.calls.append("reconnect")
        self.state = "connecting"


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, text: str) -> None:
        self.sent.append(json.loads(text))


def test_web_feishu_protocol():
    from zero.channels.web.server import WebChannel

    async def main():
        channel = object.__new__(WebChannel)
        controller = FakeController()
        channel.feishu = controller
        channel._clients = set()
        channel._loop = asyncio.get_running_loop()
        ws = FakeWS()

        await channel._on_feishu(ws, {"type": "feishu"})
        read = ws.sent[-1]
        assert read["ok"] is True and read["snapshot"]["state"] == "disabled"

        await channel._on_feishu(ws, {"type": "feishu", "op": "enable"})
        assert ws.sent[-1]["snapshot"]["state"] == "connecting"
        assert controller.calls == ["enable"]

        await channel._on_feishu(ws, {"type": "feishu", "op": "disable"})
        assert ws.sent[-1]["snapshot"]["state"] == "disabled"
        assert controller.calls == ["enable", "disable"]

        await channel._on_feishu(ws, {"type": "feishu", "op": "reconnect"})
        assert ws.sent[-1]["snapshot"]["state"] == "connecting"
        assert controller.calls == ["enable", "disable", "reconnect"]

        await channel._on_feishu(ws, {"type": "feishu", "op": "bogus"})
        bad = ws.sent[-1]
        assert bad["ok"] is False and "未知" in bad["error"]

        channel._clients = {ws}
        channel._feishu_changed(
            {"configured": True, "state": "connected", "error": ""})
        for _ in range(3):
            await asyncio.sleep(0)
        pushed = ws.sent[-1]
        assert pushed["type"] == "feishu" and pushed["op"] == "state"
        assert pushed["snapshot"]["state"] == "connected"

    asyncio.run(main())


def test_web_feishu_rejects_unconfigured_enable():
    from zero.channels.web.server import WebChannel

    async def main():
        channel = object.__new__(WebChannel)
        channel.feishu = FakeController(configured=False)
        channel._clients = set()
        ws = FakeWS()

        await channel._on_feishu(ws, {"type": "feishu", "op": "enable"})
        result = ws.sent[-1]
        assert result["ok"] is False
        assert "appId" in result["error"]
        assert result["snapshot"]["state"] == "unconfigured"

    asyncio.run(main())
