"""终止链路加固验证：

1. CancelledError 直穿工具循环（应用退出场景）也要补占位 tool 结果
2. load_history 对孤儿 tool_calls 的加载兜底修复（含幂等性）
3. SIGINT → _sigint_stop_turn 转发终止请求（渲染中 Ctrl+C 不退出应用）
"""
import asyncio
import json
import unittest
from pathlib import Path

from zero.bus.queue import MessageBus
from tests.support import make_paths, make_permission, make_scheduler_store, make_store, make_test_config
from zero.core.hooks import Hooks
from zero.core.runner import AgentRunner
from zero.models import Message, MessageBody, MessageOut, StopRequest
from zero.paths import current
from zero.session.models import TurnMarker


class TestCancelledErrorRepairsHistory(unittest.TestCase):
    """Windows Ctrl+C 退出应用时，asyncio Runner 取消所有 task，取消以
    CancelledError（而非 TurnCancelled）直穿 runner 工具循环——曾经因此
    留下孤儿 assistant(tool_calls) → 后续 400。"""

    def test_cancelled_error_during_tool_repairs_history(self):
        from zero.core.context import Context as Ctx

        class ToolsProvider:
            async def stream_chunks(self, payload, **_kw):
                yield "content", "查一下"
                yield "tool_calls", {
                    "index": 0, "id": "call_x", "type": "function",
                    "function": {"name": "foo", "arguments": "{}"}}

        async def scenario() -> list:
            context = Ctx(session_id="s-hard", channel="cli",
                          session_store=make_store(), paths=make_paths(), permission=make_permission())
            runner = AgentRunner(config=make_test_config(),
                                 running_context=context, hooks=Hooks())
            runner.provider = ToolsProvider()

            async def cancelled_exec(tc):
                raise asyncio.CancelledError()

            runner._execute_and_persist = cancelled_exec
            ag = runner.run_stream()
            await ag.__anext__()   # delta
            await ag.__anext__()   # tool_call 事件
            with self.assertRaises(asyncio.CancelledError):
                await ag.__anext__()
            # 取消上抛前必须已补占位（同步落盘，不经 await）；
            # 系统取消 ≠ 用户终止，文案必须如实区分
            return make_store().load_history("s-hard", "cli")

        hist = asyncio.run(scenario())
        self.assertEqual(len(hist), 2)
        self.assertEqual(hist[0].sender, "assistant")
        self.assertEqual(hist[0].body.tool_calls[0].id, "call_x")
        tool = hist[1]
        self.assertEqual(tool.sender, "tool")
        self.assertEqual(tool.body.tool_call_id, "call_x")
        self.assertEqual(json.loads(tool.body.content)["error"],
                         "aborted: runner cancelled (application shutdown)")


class TestLoadHistoryTurnRecovery(unittest.TestCase):
    """恢复语义由 turn 标记决定，不再靠正文文案猜测。"""

    @staticmethod
    def _write_history(path: Path, rows: list) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(r.model_dump_json() + "\n" for r in rows),
            encoding="utf-8")

    def _mk_session(self, sid: str) -> Path:
        root = current().sessions / "cli" / "2026-10-06" / sid
        # index 不可少：_session_file 依据它定位日期目录
        index = current().sessions / "cli" / "index.json"
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text(json.dumps({"cli-x": {
            "session_id": sid, "date": "2026-10-06"}}), encoding="utf-8")
        return root / "messages.jsonl"

    @staticmethod
    def _tool_call_msg(sid: str, call_id: str) -> MessageOut:
        return MessageOut(session_id=sid, channel="cli",
                          body=MessageBody(content="", tool_calls=[{
                              "id": call_id,
                              "type": "function",
                              "function": {"name": "f", "arguments": "{}"},
                          }]))

    def test_closed_turn_missing_result_repaired_once(self):
        sid = "11111111-1111-1111-1111-111111111111"
        f = self._mk_session(sid)
        self._write_history(f, [
            TurnMarker(kind="turn_begin", turn_id="t1"),
            MessageOut(session_id=sid, channel="cli",
                       body=MessageBody(content="问")),
            self._tool_call_msg(sid, "call_orphan"),
            TurnMarker(kind="turn_end", turn_id="t1"),
        ])

        # 只读路径：内存补占位，不写盘
        hist = make_store().load_history(sid, "cli")
        self.assertEqual(len(hist), 3)
        self.assertEqual(hist[2].sender, "tool")
        self.assertTrue(hist[2].synthetic)
        self.assertEqual(hist[2].body.tool_call_id, "call_orphan")
        self.assertEqual(len(f.read_text().strip().splitlines()), 4)

        # 轮次所有者：落盘修复一次，之后幂等
        hist2 = make_store().load_history(
            sid, "cli", repair_closed_turns=True)
        self.assertEqual(len(hist2), 3)
        raw = f.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(raw), 5)  # begin + 3 消息 + end
        self.assertIn('"synthetic":true', raw[3])
        hist3 = make_store().load_history(
            sid, "cli", repair_closed_turns=True)
        self.assertEqual(len(hist3), 3)
        self.assertEqual(len(f.read_text().strip().splitlines()), 5)

    def test_open_turn_is_dropped_unless_it_is_the_active_turn(self):
        """没有 turn_end 的轮次：对后续轮次是崩溃残留，绝不拼进上下文。"""
        sid = "22222222-2222-2222-2222-222222222222"
        f = self._mk_session(sid)
        self._write_history(f, [
            TurnMarker(kind="turn_begin", turn_id="t-crash"),
            Message(session_id=sid, channel="cli", sender="user",
                    recipient="assistant", body=MessageBody(content="半截问题")),
            self._tool_call_msg(sid, "call_crash"),
        ])
        before = f.read_text(encoding="utf-8")

        # 新轮次读历史：残留整轮丢弃，且不写盘
        self.assertEqual(make_store().load_history(sid, "cli"), [])
        self.assertEqual(f.read_text(encoding="utf-8"), before)

        # 该轮的 owner 自己读：原样返回，也不补占位（它可能还在写）
        own = make_store().load_history(
            sid, "cli", active_turn_id="t-crash",
            repair_closed_turns=True)
        self.assertEqual(len(own), 2)
        self.assertEqual([m.sender for m in own], ["user", "assistant"])
        self.assertEqual(f.read_text(encoding="utf-8"), before)

    def test_residue_excluded_but_closed_turns_kept(self):
        sid = "33333333-3333-3333-3333-333333333333"
        f = self._mk_session(sid)
        self._write_history(f, [
            TurnMarker(kind="turn_begin", turn_id="t-closed"),
            MessageOut(session_id=sid, channel="cli",
                       body=MessageBody(content="问")),
            MessageOut(session_id=sid, channel="cli",
                       body=MessageBody(content="答")),
            TurnMarker(kind="turn_end", turn_id="t-closed"),
            TurnMarker(kind="turn_begin", turn_id="t-crash"),
            MessageOut(session_id=sid, channel="cli",
                       body=MessageBody(content="下一问")),
            self._tool_call_msg(sid, "call_crash2"),
        ])
        hist = make_store().load_history(sid, "cli")
        self.assertEqual([m.body.content for m in hist], ["问", "答"])

    def test_clean_history_untouched(self):
        sid = "44444444-4444-4444-4444-444444444444"
        f = self._mk_session(sid)
        self._write_history(f, [
            MessageOut(session_id=sid, channel="cli",
                       body=MessageBody(content="问")),
            MessageOut(session_id=sid, channel="cli",
                       body=MessageBody(content="答")),
        ])
        before = f.read_text(encoding="utf-8")
        make_store().load_history(sid, "cli")
        self.assertEqual(f.read_text(encoding="utf-8"), before)


class TestSigintStopTurn(unittest.TestCase):
    def test_sigint_stop_turn_sends_request(self):
        from zero.channels.cli import CLIChannel

        async def scenario():
            bus = MessageBus()
            ch = CLIChannel(msg_bus=bus, session_store=make_store(), paths=make_paths(), permission=make_permission(),
                       scheduler_store=make_scheduler_store())
            ch.service.session_id = "s-sigint"
            ch._active_turn_id = "turn-1"
            ch._sigint_stop_turn("turn-1")
            req = await asyncio.wait_for(bus.stop_queue.dequeue(), 1)
            return req

        req = asyncio.run(scenario())
        self.assertIsInstance(req, StopRequest)
        self.assertEqual(req.channel, "cli")
        self.assertEqual(req.session_id, "s-sigint")
        self.assertEqual(req.turn_id, "turn-1")

    def test_sigint_notifies_approval_abort(self):
        """审批中 Ctrl+C：SIGINT 路径必须显式通知审批面板收工（它不取消
        任何 task，面板的 finally 不会执行，否则倒计时孤儿跑到 180s）。"""
        import threading

        from zero.channels.cli import CLIChannel

        async def scenario():
            bus = MessageBus()
            ch = CLIChannel(msg_bus=bus, session_store=make_store(), paths=make_paths(), permission=make_permission(),
                       scheduler_store=make_scheduler_store())
            ch.service.session_id = "s-sigint2"
            ch._active_turn_id = "turn-2"
            abort = threading.Event()
            ch.approval._abort = abort
            ch._sigint_stop_turn("turn-2")
            req = await asyncio.wait_for(bus.stop_queue.dequeue(), 1)
            return req, abort

        req, abort = asyncio.run(scenario())
        self.assertEqual(req.turn_id, "turn-2")
        self.assertTrue(abort.is_set())


if __name__ == "__main__":
    unittest.main()
