"""审批 UI 倒计时线程的 abort 行为回归验证。"""
import threading
import time
import unittest

from zero.bus.queue import MessageBus
from zero.channels.cli import CLIChannel


class TestApprovalUiAbort(unittest.TestCase):

    def test_read_key_win32_exits_immediately_on_abort(self):
        """abort 置位后，倒计时线程应在毫秒级退出，不再打印/递减。"""
        ch = CLIChannel(MessageBus())
        abort = threading.Event()
        abort.set()
        start = time.time()
        # deadline 还很远（180s），若 abort 失效会跑满倒计时
        result = ch._read_key_win32(time.time() + 180, "yna", abort)
        self.assertEqual(result, "")
        self.assertLess(time.time() - start, 1.0)

    def test_read_key_win32_no_abort_waiting_deadline(self):
        """无 abort、无按键：到 deadline 后返回 ""（fail-closed）。"""
        ch = CLIChannel(MessageBus())
        deadline = time.time() + 0.2
        start = time.time()
        result = ch._read_key_win32(deadline, "yna")
        self.assertEqual(result, "")
        self.assertGreaterEqual(time.time() - start, 0.15)


if __name__ == "__main__":
    unittest.main()
