"""Offline smoke test for the streaming pipeline (no real LLM)."""
import asyncio
import sys

from zero.core.context import Context
from zero.core.runner import AgentRunner
from zero.models import AgentRuntimeConfig, LLMConfig, ProviderConfig, StreamEvent


class FakeProvider:
    def __init__(self, *a, **kw):
        pass

    async def stream_chunks(self, payload):
        for chunk in ["你好", "，这是", "流式回复"]:
            yield "content", chunk


async def main():
    import zero.core.runner as runner_mod
    runner_mod.OpenAI = FakeProvider  # monkeypatch

    cfg = AgentRuntimeConfig(provider=ProviderConfig(api_base="https://x.local", api_key="k"),
                              llm_config=LLMConfig(model="fake"))
    ctx = Context(session_id="smoke-test-session", channel="cli")
    runner = AgentRunner(cfg, ctx)

    events = []
    async for ev in runner.run_stream():
        events.append(ev)

    kinds = [e.kind for e in events]
    assert kinds == ["delta", "delta", "delta", "done"], kinds
    assert events[-1].content == "你好，这是流式回复", events[-1].content
    text = await runner.run()
    assert text == "你好，这是流式回复", text
    print("runner stream ok:", kinds)

    # --- store roundtrip with channel=cli ---
    from zero.session.store import SessionStore
    sid = SessionStore.create_session("cli", "smoke-src")
    sessions = SessionStore.list_sessions("cli")
    assert any(s.session_id == sid for s in sessions)
    print("store ok: cli sessions =", len(sessions))

    # --- bus stream dispatch ---
    from zero.bus.queue import MessageBus
    from zero.models import MessageIn, MessageBody
    bus = MessageBus()
    sub = bus.subscribe_stream("cli")
    dispatcher = asyncio.create_task(bus.run_stream_dispatcher())
    await bus.stream_queue.enqueue(StreamEvent(channel="cli", kind="delta", content="x"))
    got = await asyncio.wait_for(sub.dequeue(), timeout=1)
    dispatcher.cancel()
    assert got.content == "x"
    print("bus stream dispatch ok")

    print("ALL SMOKE TESTS PASSED")


asyncio.run(main())
