"""AgentLoop + AgentRunner 用户终止（旁路停止信号）端到端验证。"""
import asyncio
import json
import threading
import time
import unittest

from zero.bus.queue import MessageBus
from zero.config import default_config
from zero.core.cancellation import TurnCancelled
from zero.core.loop import AgentLoop
from zero.models import MessageBody, MessageIn, StopRequest, ProviderConfig
from zero.session.store import SessionStore


class FakeProvider:
    """模拟 LLM：首段流式后挂起，测试放行后继续输出，再正常结束。"""

    def __init__(self):
        self.gate = asyncio.Event()

    async def stream_chunks(self, payload, **_kw):
        yield "content", "部分回复-"
        await self.gate.wait()
        yield "content", "更多内容"


async def main() -> dict:
    bus = MessageBus()
    cfg = default_config
    loop = AgentLoop(msg_bus=bus, config=cfg)

    from zero.core.runner import AgentRunner

    fake = FakeProvider()
    orig_init = AgentRunner.__init__

    def patched_init(self, config, running_context, task="default",
                     tool_registry=None, hooks=None, model_override=None,
                     stop_event=None):
        orig_init(self, config, running_context, task, tool_registry, hooks,
                  model_override, stop_event)
        self.provider = fake
        fake.stop_event = stop_event

    AgentRunner.__init__ = patched_init
    try:
        loop_task = asyncio.create_task(loop.run())
        await asyncio.sleep(0.1)

        msg = MessageIn(
            channel="cli", session_id="s-test-stop",
            body=MessageBody(content="hi"))
        await bus.in_queue.enqueue(msg)

        # 等到首条 delta（runner 已进入流式挂起）
        first = None
        for _ in range(100):
            ev = await asyncio.wait_for(bus.stream_queue.dequeue(), 1)
            if ev.kind == "delta":
                first = ev
                break

        # 用户终止：旁路停止信号（精确匹配 turn）
        await bus.stop_queue.enqueue(StopRequest(
            channel="cli", session_id="s-test-stop", turn_id=msg.id))
        # 确定性等待 dispatcher 置位 stop_event，再放行 LLM 剩余流 →
        # runner 在流后安全点抛 TurnCancelled（真实场景流很慢无此竞态）
        for _ in range(100):
            if fake.stop_event is not None and fake.stop_event.is_set():
                break
            await asyncio.sleep(0.01)
        assert fake.stop_event is not None and fake.stop_event.is_set(), \
            "stop_event 未被置位"
        fake.gate.set()

        # 收尾 done 事件（cancelled 标记 + 部分回复）
        done_ev = None
        for _ in range(100):
            ev = await asyncio.wait_for(bus.stream_queue.dequeue(), 1)
            if ev.kind == "done":
                done_ev = ev
                break
        # 最终 MessageOut
        msg_out = await asyncio.wait_for(bus.out_queue.dequeue(), 1)

        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
        return {
            "first_delta": first.content,
            "done_ev": done_ev,
            "msg_out": msg_out,
            "history": SessionStore.load_history("s-test-stop", "cli"),
        }
    finally:
        AgentRunner.__init__ = orig_init


class TestTurnStop(unittest.TestCase):

    def test_stop_signal_cancels_turn(self):
        result = asyncio.run(main())
        self.assertEqual(result["first_delta"], "部分回复-")
        # 收尾 done：带 cancelled 标记 + 已生成的部分回复
        self.assertEqual(result["done_ev"].kind, "done")
        self.assertTrue(result["done_ev"].metadata.get("cancelled"))
        self.assertTrue(result["done_ev"].content.startswith("部分回复-"))
        # 流内安全点：取消在下一 chunk 边界即生效，未产出的 "更多内容"
        # 不再被消费——最终 MessageOut = 已产出的部分回复
        self.assertEqual(result["msg_out"].body.content, "部分回复-")
        # 会话历史不落盘部分回复（只有用户 MessageIn）
        self.assertEqual(len(result["history"]), 1)
        self.assertEqual(result["history"][0].body.content, "hi")

    def test_cancel_after_assistant_tools_repairs_history(self):
        """取消落在『已持久化 assistant(tool_calls)』后：未执行的工具补齐
        占位 tool 结果，避免下一轮加载时 API 400。"""
        from zero.core.context import Context
        from zero.core.runner import AgentRunner

        class ToolsProvider:
            """流式返回 2 个 tool_calls 后正常结束。"""

            async def stream_chunks(self, payload, **_kw):
                yield "content", "需要查一下"
                yield "tool_calls", {
                    "index": 0, "id": "call_a", "type": "function",
                    "function": {"name": "foo", "arguments": "{}"}}
                yield "tool_calls", {
                    "index": 1, "id": "call_b", "type": "function",
                    "function": {"name": "bar", "arguments": "{}"}}

        async def main_repair() -> list:
            cfg = default_config
            context = Context(session_id="s-repair", channel="cli")
            stop_event = asyncio.Event()
            runner = AgentRunner(config=cfg, running_context=context,
                                 stop_event=stop_event)
            runner.provider = ToolsProvider()

            # 门控第一个工具的执行：取消信号在『第一个工具执行中』到达，
            # 确定性覆盖 已持久化 assistant-with-tools、部分工具未执行 的窗口
            gate = asyncio.Event()
            orig_exec = runner._execute_and_persist

            async def gated(tc):
                await gate.wait()
                await orig_exec(tc)

            runner._execute_and_persist = gated
            try:
                ag = runner.run_stream()
                ev1 = await ag.__anext__()      # delta
                ev2 = await ag.__anext__()      # tool_call foo
                assert ev1.kind == "delta" and ev2.kind == "tool_call"
                stop_event.set()                # 用户在此刻终止
                gate.set()                      # 放行第一个工具 → 落盘
                with self.assertRaises(TurnCancelled):
                    await ag.__anext__()
            finally:
                runner._execute_and_persist = orig_exec
            return SessionStore.load_history("s-repair", "cli")

        hist = asyncio.run(main_repair())
        # [assistant(tool_calls a,b), tool(a), tool(b 占位)]
        self.assertEqual(len(hist), 3)
        asst = hist[0]
        self.assertEqual(asst.sender, "assistant")
        self.assertEqual(len(asst.body.tool_calls), 2)
        ids = [tc.id for tc in asst.body.tool_calls]
        self.assertEqual(ids, ["call_a", "call_b"])
        # 每个 tool_call_id 都有对应 tool 消息（协议合法）
        tool_ids = [m.body.tool_call_id for m in hist[1:] if m.sender == "tool"]
        self.assertEqual(tool_ids, ["call_a", "call_b"])
        # 未执行的 call_b 是占位结果
        placeholders = [m for m in hist if m.sender == "tool"
                        and m.body.tool_call_id == "call_b"]
        self.assertEqual(len(placeholders), 1)
        body = json.loads(placeholders[0].body.content)
        self.assertEqual(body["error"], "turn cancelled by user")


class TestStopDuringSilentThinking(unittest.TestCase):
    """复现用户报障：思考期（长时间无任何 chunk）终止后 turn 悬挂、
    下一轮卡 thinking。

    用真实 OpenAI provider + stub 慢速 SSE 响应覆盖：首条输出后长时间
    静默（reasoner 思考期），stop_event 必须打断挂起的 queue.get()，
    而不是等思考期结束（chunk 边界检查到不了）。
    """

    def test_stop_during_silence_then_next_turn_ok(self):
        from zero.core.runner import AgentRunner
        from zero.providers.openai import OpenAI

        class SlowThinkingResp:
            """stub SSE：首条后静默 think 秒（模拟思考期），close 打断。"""

            def __init__(self, think: float):
                self._closed = threading.Event()
                self._think = think

            def raise_for_status(self):
                pass

            def iter_lines(self):
                yield ('data: {"choices":[{"delta":{"content":"部分"}}]}'
                       ).encode()
                if not self._closed.wait(self._think):
                    yield ('data: {"choices":[{"delta":{"content":"完整"}}]}'
                           ).encode()
                    yield b'data: [DONE]'

            def close(self):
                self._closed.set()

        async def scenario() -> dict:
            bus = MessageBus()
            loop = AgentLoop(msg_bus=bus, config=default_config)

            prov = OpenAI(ProviderConfig(api_base="http://stub", api_key="k"))
            # turn1 思考期 3s（终止应打断它）；turn2 立即完整输出
            responses = iter([SlowThinkingResp(3.0), SlowThinkingResp(0.0)])
            prov._request_with_retry = lambda *a, **k: next(responses)

            orig_init = AgentRunner.__init__

            def patched_init(self, config, running_context, task="default",
                             tool_registry=None, hooks=None,
                             model_override=None, stop_event=None):
                orig_init(self, config, running_context, task,
                          tool_registry, hooks, model_override, stop_event)
                self.provider = prov

            AgentRunner.__init__ = patched_init
            try:
                loop_task = asyncio.create_task(loop.run())
                await asyncio.sleep(0.1)

                # ---- turn 1：终止打断思考期 ----
                msg1 = MessageIn(channel="cli", session_id="s-silence",
                                 body=MessageBody(content="q1"))
                await bus.in_queue.enqueue(msg1)
                for _ in range(100):
                    ev = await asyncio.wait_for(
                        bus.stream_queue.dequeue(), 1)
                    if ev.kind == "delta":
                        break
                t0 = time.monotonic()
                await bus.stop_queue.enqueue(StopRequest(
                    channel="cli", session_id="s-silence", turn_id=msg1.id))
                done1 = None
                for _ in range(100):
                    ev = await asyncio.wait_for(
                        bus.stream_queue.dequeue(), 2)
                    if ev.kind == "done":
                        done1 = ev
                        break
                stop_elapsed = time.monotonic() - t0
                out1 = await asyncio.wait_for(bus.out_queue.dequeue(), 2)

                # ---- turn 2：终止后重新提问，不卡 thinking ----
                msg2 = MessageIn(channel="cli", session_id="s-silence",
                                 body=MessageBody(content="q2"))
                await bus.in_queue.enqueue(msg2)
                out2 = await asyncio.wait_for(
                    bus.out_queue.dequeue(), 5)

                loop_task.cancel()
                try:
                    await loop_task
                except asyncio.CancelledError:
                    pass
                return {"done1": done1, "stop_elapsed": stop_elapsed,
                        "out1": out1, "out2": out2}
            finally:
                AgentRunner.__init__ = orig_init

        r = asyncio.run(scenario())
        # 终止打断挂起的思考期（3s 静默），不等流自然结束
        self.assertLess(r["stop_elapsed"], 1.5,
                        f"stop 未打断思考期: {r['stop_elapsed']:.2f}s")
        self.assertTrue(r["done1"].metadata.get("cancelled"))
        self.assertEqual(r["out1"].body.content, "部分")
        # 下一轮正常处理（用户报障：此前卡 thinking 永无输出）
        self.assertEqual(r["out2"].body.content, "部分完整")


if __name__ == "__main__":
    unittest.main()
