"""终止链路加固验证：

1. CancelledError 直穿工具循环（应用退出场景）也要补占位 tool 结果
2. load_history 对孤儿 tool_calls 的加载兜底修复（含幂等性）
3. SIGINT → _sigint_stop_turn 转发终止请求（渲染中 Ctrl+C 不退出应用）
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from zero.bus.queue import MessageBus
from zero.config import default_config
from zero.core.context import Context
from zero.core.runner import AgentRunner
from zero.models import MessageBody, MessageOut, StopRequest
from zero.session import store as store_mod
from zero.session.store import SessionStore


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

        async def scenario(tmp: Path) -> list:
            store_mod.SESSIONS_ROOT = tmp / "sessions"
            context = Ctx(session_id="s-hard", channel="cli")
            runner = AgentRunner(config=default_config,
                                 running_context=context)
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
            return SessionStore.load_history("s-hard", "cli")

        with tempfile.TemporaryDirectory() as tmp:
            hist = asyncio.run(scenario(Path(tmp)))
        self.assertEqual(len(hist), 2)
        self.assertEqual(hist[0].sender, "assistant")
        self.assertEqual(hist[0].body.tool_calls[0].id, "call_x")
        tool = hist[1]
        self.assertEqual(tool.sender, "tool")
        self.assertEqual(tool.body.tool_call_id, "call_x")
        self.assertEqual(json.loads(tool.body.content)["error"],
                         "aborted: runner cancelled (application shutdown)")


class TestLoadHistoryOrphanRepair(unittest.TestCase):
    """load_history 兜底：任何来源的孤儿 tool_calls 在加载时治愈。"""

    @staticmethod
    def _write_history(path: Path, rows: list[MessageOut]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(r.model_dump_json() + "\n" for r in rows),
            encoding="utf-8")

    def _mk_session(self, tmp: Path, sid: str) -> Path:
        store_mod.SESSIONS_ROOT = tmp / "sessions"
        root = tmp / "sessions" / "cli" / "2026-10-06" / sid
        # index 不可少：_session_file 依据它定位日期目录
        index = tmp / "sessions" / "cli" / "index.json"
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text(json.dumps({"cli-x": {
            "session_id": sid, "date": "2026-10-06"}}), encoding="utf-8")
        return root / "messages.jsonl"

    def test_orphan_repaired_and_idempotent(self):
        sid = "11111111-1111-1111-1111-111111111111"
        with tempfile.TemporaryDirectory() as tmp:
            f = self._mk_session(Path(tmp), sid)
            self._write_history(f, [
                MessageOut(session_id=sid, channel="cli",
                           body=MessageBody(content="问")),
                # 孤儿：assistant(tool_calls) 后直接是下一条 user
                MessageOut(session_id=sid, channel="cli",
                           body=MessageBody(content="",
                                            tool_calls=[{
                                                "id": "call_orphan",
                                                "type": "function",
                                                "function": {"name": "f",
                                                             "arguments": "{}"},
                                            }])),
                MessageOut(session_id=sid, channel="cli",
                           body=MessageBody(content="继续")),
            ])
            hist = SessionStore.load_history(sid, "cli")
            # [user, assistant(tool_calls), tool(占位), user]
            self.assertEqual(len(hist), 4)
            self.assertEqual(hist[2].sender, "tool")
            self.assertEqual(hist[2].body.tool_call_id, "call_orphan")
            # 加载修复不知道原始原因，文案中性（不谎称用户取消）
            self.assertEqual(json.loads(hist[2].body.content)["error"],
                             "missing tool result (auto-repaired at load)")
            # 修复已落盘；再次加载幂等（不重复补占位）
            hist2 = SessionStore.load_history(sid, "cli")
            self.assertEqual(len(hist2), 4)
            raw = f.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(raw), 4)

    def test_clean_history_untouched(self):
        sid = "22222222-2222-2222-2222-222222222222"
        with tempfile.TemporaryDirectory() as tmp:
            f = self._mk_session(Path(tmp), sid)
            self._write_history(f, [
                MessageOut(session_id=sid, channel="cli",
                           body=MessageBody(content="问")),
                MessageOut(session_id=sid, channel="cli",
                           body=MessageBody(content="答")),
            ])
            before = f.read_text(encoding="utf-8")
            SessionStore.load_history(sid, "cli")
            self.assertEqual(f.read_text(encoding="utf-8"), before)


class TestSigintStopTurn(unittest.TestCase):
    def test_sigint_stop_turn_sends_request(self):
        from zero.channels.cli import CLIChannel

        async def scenario():
            bus = MessageBus()
            ch = CLIChannel(msg_bus=bus)
            ch.session_id = "s-sigint"
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
            ch = CLIChannel(msg_bus=bus)
            ch.session_id = "s-sigint2"
            ch._active_turn_id = "turn-2"
            abort = threading.Event()
            ch._approval_abort = abort
            ch._sigint_stop_turn("turn-2")
            req = await asyncio.wait_for(bus.stop_queue.dequeue(), 1)
            return req, abort

        req, abort = asyncio.run(scenario())
        self.assertEqual(req.turn_id, "turn-2")
        self.assertTrue(abort.is_set())


if __name__ == "__main__":
    unittest.main()
