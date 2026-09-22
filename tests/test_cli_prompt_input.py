"""Regression test: _read_input's prompt_toolkit path must actually work.

The original implementation passed patch_stdout=True to
PromptSession.prompt_async(), which has no such kwarg on prompt_toolkit
3.x — the TypeError killed the CLI channel right after the user pressed
Enter to enter a session. This test drives the real _read_input through
a pipe input + dummy output so a signature mismatch fails here first.

Run: python tests/test_cli_prompt_input.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prompt_toolkit import PromptSession
from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from zero.bus.queue import MessageBus
from zero.channels.cli import CLIChannel


async def _read_once(ch, typed: str) -> str:
    # create_app_session 让 patch_stdout 的 StdoutProxy 拿到 DummyOutput，
    # 否则在无控制台的环境下它会尝试从 sys.stdout 创建 Win32Output 而失败
    with create_app_session(output=DummyOutput()):
        with create_pipe_input() as pipe:
            ch._prompt = PromptSession(input=pipe, output=DummyOutput())
            pipe.send_text(typed + "\n")
            return await asyncio.wait_for(ch._read_input(), timeout=5)


async def main():
    ch = CLIChannel(MessageBus())
    # 强制走 ptk 分支（CI/管道环境下 _use_ptk 为 False）
    text = await _read_once(ch, "你好，定时任务")
    assert text == "你好，定时任务", repr(text)

    # 未读指示符路径也要能正常调用（prompt 文案带 ⏰N）
    ch._unread = 2
    text = await _read_once(ch, "ok")
    assert text == "ok", repr(text)

    # 提示符活跃期间，异步结果打印走 ptk 原生管线路径且不报错
    import io
    from rich.console import Console
    from rich.panel import Panel

    ch._use_ptk = True
    ch.console = Console(file=io.StringIO(), highlight=False, width=100)
    with create_app_session(output=DummyOutput()):
        with create_pipe_input() as pipe:
            ch._prompt = PromptSession(input=pipe, output=DummyOutput())
            prompt_task = asyncio.create_task(ch._read_input())
            await asyncio.sleep(0.3)  # 等 ptk app 跑起来
            assert ch._prompt_active
            ch._print_above_prompt(Panel(
                "Ace, time to drink water.",
                title="zero · ⏰ 定时任务", border_style="yellow"))
            await asyncio.sleep(0.5)  # 让 run_in_terminal 执行完
            pipe.send_text("done\n")
            text = await asyncio.wait_for(prompt_task, timeout=5)
            assert text == "done", repr(text)
            assert not ch._prompt_active
    print("print above prompt ok")

    print("prompt input path ok (with and without unread indicator)")
    print("ALL PROMPT INPUT TESTS PASSED")


asyncio.run(main())
