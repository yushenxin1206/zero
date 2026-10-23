"""Regression test: CliInput.read's prompt_toolkit path must actually work.

The original implementation passed patch_stdout=True to
PromptSession.prompt_async(), which has no such kwarg on prompt_toolkit
3.x — the TypeError killed the CLI channel right after the user pressed
Enter to enter a session. This test drives the real input path through a
pipe input + dummy output so a signature mismatch fails here first.
"""
import asyncio
import io

from prompt_toolkit import PromptSession
from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from rich.panel import Panel

from zero.bus.queue import MessageBus
from zero.channels.cli import CLIChannel


async def _read_once(ch, typed: str) -> str:
    # create_app_session 让 patch_stdout 的 StdoutProxy 拿到 DummyOutput，
    # 否则在无控制台的环境下它会尝试从 sys.stdout 创建 Win32Output 而失败
    with create_app_session(output=DummyOutput()):
        with create_pipe_input() as pipe:
            ch.input._prompt = PromptSession(input=pipe, output=DummyOutput())
            pipe.send_text(typed + "\n")
            return await asyncio.wait_for(ch.input.read(), timeout=5)


def test_prompt_toolkit_input_path():
    ch = CLIChannel(MessageBus())

    async def scenario():
        # 强制走 ptk 分支（CI/管道环境下 _use_ptk 为 False）
        text = await _read_once(ch, "你好，定时任务")
        assert text == "你好，定时任务", repr(text)

        # 未读指示符路径也要能正常调用（prompt 文案带 ⏰N）
        ch.input._unread = 2
        text = await _read_once(ch, "ok")
        assert text == "ok", repr(text)

    asyncio.run(scenario())


def test_print_above_prompt_while_prompt_active():
    ch = CLIChannel(MessageBus())
    ch.renderer.console = Console(file=io.StringIO(), highlight=False, width=100)

    async def scenario():
        with create_app_session(output=DummyOutput()):
            with create_pipe_input() as pipe:
                ch.input._prompt = PromptSession(input=pipe, output=DummyOutput())
                prompt_task = asyncio.create_task(ch.input.read())
                await asyncio.sleep(0.3)  # 等 ptk app 跑起来
                assert ch.input.active
                ch.renderer.print_above_prompt(
                    Panel("Ace, time to drink water.",
                          title="zero · ⏰ 定时任务", border_style="yellow"),
                    prompt_active=ch.input.active)
                await asyncio.sleep(0.5)  # 让 run_in_terminal 执行完
                pipe.send_text("done\n")
                text = await asyncio.wait_for(prompt_task, timeout=5)
                assert text == "done", repr(text)
                assert not ch.input.active

    asyncio.run(scenario())
