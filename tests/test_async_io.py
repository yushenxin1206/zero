"""P1-2：主链路 IO 不阻塞事件循环。"""
import asyncio
import json
import time
from unittest.mock import patch

from zero.bus.queue import MessageBus
from zero.core.hooks import Hooks
from zero.core.loop import AgentLoop
from zero.models import MessageBody, MessageIn, StopRequest
from zero.session.store import SessionStore
from tests.support import make_paths, make_store, make_test_config


class _EchoProvider:
    async def stream_chunks(self, payload, **_kw):
        yield "content", "ok"


def _patch_provider(provider) -> patch:
    """替换 AgentRunner 的 provider（与 test_turn_stop 同一手法）。"""
    from zero.core.runner import AgentRunner

    orig_init = AgentRunner.__init__

    def patched_init(self, config, running_context, task="default",
                     tool_registry=None, *, hooks, model_override=None,
                     stop_event=None):
        orig_init(self, config, running_context, task, tool_registry,
                  hooks=hooks, model_override=model_override,
                  stop_event=stop_event)
        self.provider = provider

    return patch.object(AgentRunner, "__init__", patched_init)


def test_event_loop_stays_responsive_during_slow_session_write():
    """慢写入（begin_turn 睡 0.4s）期间，事件循环仍能跑心跳协程。"""

    async def scenario() -> int:
        bus = MessageBus()
        loop = AgentLoop(
            msg_bus=bus, config=make_test_config(), hooks=Hooks(),
            session_store=make_store(), paths=make_paths())
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        loop_task = asyncio.create_task(loop.run())
        await asyncio.sleep(0.05)

        real_begin = SessionStore.begin_turn

        def slow_begin(store_self, channel, session_id, turn_id):
            time.sleep(0.4)
            real_begin(store_self, channel, session_id, turn_id)

        with _patch_provider(_EchoProvider()):
            with patch.object(SessionStore, "begin_turn", slow_begin):
                await bus.in_queue.enqueue(MessageIn(
                    channel="cli", session_id="s-slow-write",
                    body=MessageBody(content="hi")))
                start = ticks
                # 仍处于 0.4s 的慢写入窗口内
                await asyncio.sleep(0.25)
                during = ticks - start
                # provider 补丁必须留到 runner 真正跑起来（慢写入之后）
                await asyncio.wait_for(bus.out_queue.dequeue(), 2)

        beat.cancel()
        loop_task.cancel()
        for task in (beat, loop_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
        return during

    # 10ms 心跳在 250ms 内应有 ~25 次；被同步写卡住时会是 0 次
    assert asyncio.run(scenario()) >= 10


def test_stop_takes_effect_during_slow_write():
    """慢写入期间发出停止信号：信号立即命中，轮次以 cancelled 闭合。"""

    async def scenario() -> dict:
        bus = MessageBus()
        loop = AgentLoop(
            msg_bus=bus, config=make_test_config(), hooks=Hooks(),
            session_store=make_store(), paths=make_paths())
        loop_task = asyncio.create_task(loop.run())
        await asyncio.sleep(0.05)

        real_begin = SessionStore.begin_turn

        def slow_begin(store_self, channel, session_id, turn_id):
            time.sleep(0.5)
            real_begin(store_self, channel, session_id, turn_id)

        with _patch_provider(_EchoProvider()):
            with patch.object(SessionStore, "begin_turn", slow_begin):
                msg = MessageIn(
                    channel="cli", session_id="s-stop-slow-write",
                    body=MessageBody(content="hi"))
                await bus.in_queue.enqueue(msg)
                await asyncio.sleep(0.05)
                await bus.stop_queue.enqueue(StopRequest(
                    channel="cli", session_id="s-stop-slow-write",
                    turn_id=msg.id))

                # 写入尚未结束（0.5s）时，停止信号就应已置位 stop_event
                hit = False
                for _ in range(30):
                    event = loop._current_stop_event
                    if event is not None and event.is_set():
                        hit = True
                        break
                    await asyncio.sleep(0.01)

        raw_lines = []
        path = make_store()._session_file("cli", "s-stop-slow-write")
        for _ in range(200):
            if path.exists():
                raw_lines = path.read_text(encoding="utf-8").strip().splitlines()
                if raw_lines:
                    last = json.loads(raw_lines[-1])
                    if last.get("kind") == "turn_end":
                        break
            await asyncio.sleep(0.02)

        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
        return {
            "hit": hit,
            "first": json.loads(raw_lines[0]) if raw_lines else {},
            "last": json.loads(raw_lines[-1]) if raw_lines else {},
        }

    result = asyncio.run(scenario())
    assert result["hit"], "停止信号在慢写入期间没有被处理"
    assert result["first"].get("kind") == "turn_begin"
    assert result["last"].get("kind") == "turn_end"
    assert result["last"].get("status") == "cancelled"
