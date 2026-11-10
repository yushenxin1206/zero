"""Offline smoke tests for the streaming pipeline (no real LLM)."""
import asyncio

import zero.core.runner as runner_mod
from zero.config import Config
from zero.core.context import Context
from zero.core.hooks import Hooks
from zero.core.runner import AgentRunner
from zero.models import StreamEvent
from zero.session.store import SessionStore


class FakeProvider:
    def __init__(self, *a, **kw):
        pass

    async def stream_chunks(self, payload, **_kw):
        for chunk in ["你好", "，这是", "流式回复"]:
            yield "content", chunk


def make_config() -> Config:
    return Config.model_validate({
        "models": {"fake": {"provider": "deepseek", "contextWindowTokens": 100000}},
        "agentRuntimes": {"default": {"llmCallOptions": {"model": "fake"}}},
        "providers": {"deepseek": {"apiBase": "https://x.local", "apiKey": "k"}},
    })


def test_runner_stream(monkeypatch):
    monkeypatch.setattr(runner_mod, "OpenAI", FakeProvider)
    cfg = make_config()
    ctx = Context(session_id="smoke-test-session", channel="cli")
    runner = AgentRunner(cfg, ctx, hooks=Hooks())

    async def scenario():
        events = [ev async for ev in runner.run_stream()]
        text = await runner.run()
        return events, text

    events, text = asyncio.run(scenario())
    kinds = [e.kind for e in events]
    assert kinds == ["delta", "delta", "delta", "done"], kinds
    assert events[-1].content == "你好，这是流式回复", events[-1].content
    assert text == "你好，这是流式回复", text


def test_store_roundtrip():
    sid = SessionStore.create_session("cli", "smoke-src")
    sessions = SessionStore.list_sessions("cli")
    assert any(s.session_id == sid for s in sessions)


def test_bus_stream_dispatch():
    from zero.bus.queue import MessageBus

    async def scenario():
        bus = MessageBus()
        sub = bus.subscribe_stream("cli")
        dispatcher = asyncio.create_task(bus.run_stream_dispatcher())
        try:
            await bus.stream_queue.enqueue(
                StreamEvent(channel="cli", kind="delta", content="x"))
            return await asyncio.wait_for(sub.dequeue(), timeout=1)
        finally:
            dispatcher.cancel()

    got = asyncio.run(scenario())
    assert got.content == "x"
