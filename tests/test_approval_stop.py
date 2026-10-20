"""审批等待与用户终止竞争的回归验证。"""
import asyncio
import unittest

from zero.bus.queue import MessageBus
from zero.core.cancellation import TurnCancelled
from zero.core.hooks import HookChain, HookContext
from zero.models import ApprovalOption
from zero.sandbox_guard import request

_OPTIONS = (
    ApprovalOption(key="y", label="允许一次"),
    ApprovalOption(key="n", label="拒绝"),
)


class TestApprovalStop(unittest.TestCase):

    def test_request_raises_turn_cancelled_when_stop_fires(self):
        """等待审批期间 stop 置位 → 立即抛 TurnCancelled，不等 TIMEOUT。"""
        bus = MessageBus()
        stop = asyncio.Event()

        async def main():
            ctx = HookContext(channel="cli", session_id="s", turn_id="t",
                              stop_event=stop)
            req_task = asyncio.create_task(
                request(bus, ctx, title="shell 命令审批", summary="echo hi",
                        detail="test", options=_OPTIONS))
            await asyncio.sleep(0)  # 让 request 注册并进入等待
            stop.set()               # 模拟用户终止
            with self.assertRaises(TurnCancelled):
                await req_task
        asyncio.run(main())

    def test_hook_chain_propagates_turn_cancelled(self):
        """HookChain._safe 不得吞掉 TurnCancelled（fail-open 的例外）。"""
        chain = HookChain()

        async def exploding(*args):
            raise TurnCancelled()
        chain.on_before_tool_call(exploding)

        async def main():
            ctx = HookContext(channel="cli", session_id="s", turn_id="t")
            with self.assertRaises(TurnCancelled):
                await chain.before_tool_call("shell", {}, ctx)
        asyncio.run(main())


if __name__ == "__main__":
    unittest.main()
