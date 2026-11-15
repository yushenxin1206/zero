"""扫码连接飞书：二维码事件、令牌校验、取消与 Web 协议。"""

import asyncio
import json

import pytest

from lark_oapi.scene.registration import AppAccessDeniedError, AppExpiredError

from zero.channels.feishu import registration
from zero.channels.feishu.control import FeishuController
from zero.channels.feishu.registration import QR_ACCOUNT_TEXT, QrConnector
from zero.models import FeishuConfig


class FakeManager:
    def __init__(self):
        self.credentials = []
        self.enabled = []

    def set_feishu_credentials(self, app_id: str, app_secret: str) -> None:
        self.credentials.append((app_id, app_secret))

    def set_feishu_enabled(self, enabled: bool) -> None:
        self.enabled.append(enabled)


def _connector(register, probe=None, manager=None):
    events = []
    manager = manager or FakeManager()
    connector = QrConnector(
        manager, events.append,
        register=register,
        probe=probe or (lambda app_id, app_secret: (True, "")),
    )
    return connector, manager, events


def test_success_emits_local_qr_then_persists():
    """二维码在本地生成；令牌校验通过后才写配置，secret 不出现在事件里。"""
    captured = {}

    async def register(on_qr_code, addons, source):
        captured["addons"] = addons
        captured["source"] = source
        on_qr_code({
            "url": "https://accounts.feishu.cn/page/launcher?ticket=abc",
            "expire_in": 120,
        })
        await asyncio.sleep(0)
        return {"client_id": "cli_x", "client_secret": "sec_x"}

    async def main():
        connector, manager, events = _connector(register)
        await connector.start()
        await asyncio.sleep(0.05)

        assert [e["phase"] for e in events] == ["waiting", "success"]
        waiting = events[0]
        assert waiting["expireIn"] == 120
        assert "ticket=abc" in waiting["url"]
        # 后端生成的 SVG，不依赖任何 CDN
        assert waiting["svg"].lstrip().startswith(("<?xml", "<svg"))
        assert manager.credentials == [("cli_x", "sec_x")]
        assert events[1]["appId"] == "cli_x"
        assert events[1]["text"].endswith("重启 zero 后生效")
        assert "sec_x" not in json.dumps(events, ensure_ascii=False)

    asyncio.run(main())

    # 预填权限与事件：够收发消息，别的不多申请
    addons = captured["addons"]
    assert "im:message:send_as_bot" in addons["scopes"]["tenant"]
    assert addons["events"]["items"]["tenant"] == ["im.message.receive_v1"]
    assert captured["source"] == "zero-agent"


def test_token_failure_discards_credentials():
    """个人版账号能扫码，但拿不到令牌：不落盘，给明确中文提示。"""

    async def register(on_qr_code, addons, source):
        return {"client_id": "cli_x", "client_secret": "sec_x"}

    async def main():
        connector, manager, events = _connector(
            register, probe=lambda app_id, app_secret: (False, "10003: nope"))
        await connector.start()
        await asyncio.sleep(0.05)

        assert events[-1] == {"phase": "failed", "text": QR_ACCOUNT_TEXT}
        assert manager.credentials == []

    asyncio.run(main())


@pytest.mark.parametrize("error, text", [
    (AppExpiredError("expired_token", ""), registration.QR_EXPIRED_TEXT),
    (AppAccessDeniedError("access_denied", ""), registration.QR_DENIED_TEXT),
])
def test_sdk_errors_are_translated(error, text):
    async def register(on_qr_code, addons, source):
        raise error

    async def main():
        connector, manager, events = _connector(register)
        await connector.start()
        await asyncio.sleep(0.05)

        assert events == [{"phase": "failed", "text": text}]
        assert manager.credentials == []

    asyncio.run(main())


def test_cancel_stops_the_background_task():
    started = asyncio.Event()

    async def register(on_qr_code, addons, source):
        started.set()
        await asyncio.sleep(3600)

    async def main():
        connector, _, events = _connector(register)
        await connector.start()
        await started.wait()

        await connector.cancel()

        assert connector.active is False
        assert events[-1]["phase"] == "cancelled"

    asyncio.run(main())


def test_start_again_replaces_the_previous_session():
    calls = []

    async def register(on_qr_code, addons, source):
        calls.append(1)
        await asyncio.sleep(3600)

    async def main():
        connector, _, events = _connector(register)
        await connector.start()
        await asyncio.sleep(0.01)
        await connector.start()
        await asyncio.sleep(0.01)

        assert len(calls) == 2
        # 重新生成时旧会话被静默取消，界面不会闪一下「已取消扫码」
        assert all(e["phase"] != "cancelled" for e in events)
        await connector.cancel()

    asyncio.run(main())


def test_controller_marks_restart_required_after_success():
    """扫码成功后当前进程仍用旧配置，快照要明确「重启后生效」。"""
    manager = FakeManager()
    controller = FeishuController(FeishuConfig(), None, None, manager)
    snapshots, qr_events = [], []
    controller.bind(snapshots.append, qr_events.append)

    async def register(on_qr_code, addons, source):
        return {"client_id": "cli_x", "client_secret": "sec_x"}

    controller._qr._register = register
    controller._qr._probe = lambda app_id, app_secret: (True, "")

    async def main():
        await controller.start_qr()
        await asyncio.sleep(0.05)

        assert qr_events[-1]["phase"] == "success"
        assert controller.snapshot()["restartRequired"] is True
        assert snapshots[-1]["restartRequired"] is True
        assert manager.credentials == [("cli_x", "sec_x")]

    asyncio.run(main())


class FakeQrController:
    def __init__(self):
        self.calls = []
        self.state = "unconfigured"
        self.error = ""

    def bind(self, on_change, on_qr=None) -> None:
        pass

    def snapshot(self):
        return {
            "configured": False, "missing": ["appId"], "state": self.state,
            "error": self.error, "restartRequired": False,
        }

    async def start_qr(self) -> None:
        self.calls.append("qr-start")

    async def cancel_qr(self) -> None:
        self.calls.append("qr-cancel")


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, text: str) -> None:
        self.sent.append(json.loads(text))


def test_web_qr_protocol_and_broadcast():
    from zero.channels.web.server import WebChannel

    async def main():
        channel = object.__new__(WebChannel)
        channel.feishu = FakeQrController()
        channel._clients = set()
        channel._loop = asyncio.get_running_loop()
        ws = FakeWS()

        await channel._on_feishu(ws, {"type": "feishu", "op": "qr-start"})
        assert ws.sent[-1]["ok"] is True
        await channel._on_feishu(ws, {"type": "feishu", "op": "qr-cancel"})
        assert channel.feishu.calls == ["qr-start", "qr-cancel"]

        channel._clients = {ws}
        channel._feishu_qr({"phase": "waiting", "url": "https://x"})
        for _ in range(3):
            await asyncio.sleep(0)
        pushed = ws.sent[-1]
        assert pushed["type"] == "feishu" and pushed["op"] == "qr"
        assert pushed["qr"]["phase"] == "waiting"

    asyncio.run(main())
