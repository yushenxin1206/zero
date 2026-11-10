"""审批终止的端到端回归：
hook 在 before_tool_call 抛 TurnCancelled（等价审批被终止后在其等待处
抛出的实际传播路径），验证：
1) run_stream 不把它吞成 error，而是放行成 cancelled 收尾；
2) 历史里已落盘 assistant(tool_calls) 的所有 tool_call_id 都补占位结果，
   下一轮不再因孤儿 tool_call_id 触发 400。
"""
import asyncio
import unittest

from zero.bus.queue import MessageBus
from zero.core.cancellation import TurnCancelled
from zero.core.hooks import HookContext, Hooks
from zero.core.loop import AgentLoop
from zero.models import MessageBody, MessageIn
from zero.session.store import SessionStore
from zero.tools.base import Tool
from zero.tools.registry import ToolRegistry
from tests.support import make_test_config, wait_turn_closed


class _DummyTool(Tool):
    name = "dummy"
    description = "dummy tool"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def run(self, context=None, **kwargs):
        return {"ok": True}


class FakeProvider:
    """模拟 LLM：多轮迭代第一轮含 tool_calls，后续无（会被终止中断）。"""

    async def stream_chunks(self, payload, **_kw):
        yield "content", ""
        yield "tool_calls", {
            "index": 0, "id": "call_terminated",
            "type": "function",
            "function": {"name": "dummy", "arguments": '{"x": 1}'},
        }


async def run_case() -> dict:
    bus = MessageBus()
    cfg = make_test_config()
    registry = ToolRegistry()
    registry.register(_DummyTool())

    class CancellingHooks(Hooks):
        async def before_tool_call(self, tool_name, kwargs, ctx: HookContext):
            raise TurnCancelled()

    loop = AgentLoop(msg_bus=bus, config=cfg,
                     tool_registry=registry, hooks=CancellingHooks())

    from zero.core.runner import AgentRunner
    orig = AgentRunner.__init__

    def patched(self, config, running_context, task="default",
                tool_registry=None, hooks=None, model_override=None,
                stop_event=None):
        orig(self, config, running_context, task, tool_registry, hooks,
             model_override, stop_event)
        self.provider = FakeProvider()

    AgentRunner.__init__ = patched
    try:
        loop_task = asyncio.create_task(loop.run())
        msg = MessageIn(channel="cli", session_id="s-approve-stop",
                        body=MessageBody(content="run cmd"))
        await bus.in_queue.enqueue(msg)

        # 收尾 done，应带 cancelled 标记（而非 error）
        done_ev = None
        err_ev = None
        for _ in range(100):
            ev = await asyncio.wait_for(bus.stream_queue.dequeue(), 1)
            if ev.kind == "done":
                done_ev = ev
                break
            if ev.kind == "error":
                err_ev = ev
                break
        try:
            msg_out = await asyncio.wait_for(bus.out_queue.dequeue(), 1)
        except Exception:
            msg_out = None
        # 取消收尾先把 done/MessageOut 入队、再落 end_turn；不等闭合就读历史
        # 会把本轮当成崩溃残留（见 SessionStore.load_history）
        await wait_turn_closed("cli", "s-approve-stop", msg.id)
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass

        history = SessionStore.load_history("s-approve-stop", "cli")
        return {"done_ev": done_ev, "err_ev": err_ev, "msg_out": msg_out,
                "history": history}
    finally:
        AgentRunner.__init__ = orig


class TestApprovalTerminate(unittest.TestCase):

    def test_approval_cancel_persists_placeholder(self):
        r = asyncio.run(run_case())
        # 终止信号切达 cancelled 收尾，而非 error
        self.assertIsNone(r["err_ev"], "TurnCancelled 不应被吞成 error")
        self.assertIsNotNone(r["done_ev"])
        self.assertTrue(r["done_ev"].metadata.get("cancelled"))
        # 最终 MessageOut 走取消路径（不落盘部分回复）
        # 历史协议合法：每个 assistant.tool_calls.id 都有配对 tool 结果
        tool_ids = {m.body.tool_call_id for m in r["history"]
                    if m.sender == "tool" and m.body.tool_call_id}
        listed = {tc.id for m in r["history"] if m.sender == "assistant"
                  and m.body.tool_calls for tc in m.body.tool_calls}
        self.assertEqual(listed, tool_ids,
                         f"孤儿 tool_call_id: {listed - tool_ids}")
        # 至少记录了 assistant(tool_calls) 和占位 tool
        senders = [m.sender for m in r["history"]]
        self.assertIn("tool", senders)


if __name__ == "__main__":
    unittest.main()