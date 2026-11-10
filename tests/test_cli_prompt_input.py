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
from tests.support import make_scheduler_store, make_store


class RecordingOutput(DummyOutput):
    """捕获 ptk 渲染输出，供测试等待「流里出现某段内容」这一事实。"""

    def __init__(self) -> None:
        self.buffer = ""
        self.written = asyncio.Event()

    def write(self, data: str) -> None:
        self.buffer += data
        self.written.set()

    def write_raw(self, data: str) -> None:
        self.buffer += data
        self.written.set()


async def _wait_output(output: RecordingOutput, needle: str,
                       after: int = 0) -> int:
    """等到输出流在 after 之后出现 needle，返回其位置（不轮询、不等时长）。"""
    while True:
        pos = output.buffer.find(needle, after)
        if pos >= 0:
            return pos
        output.written.clear()
        pos = output.buffer.find(needle, after)
        if pos >= 0:
            return pos
        try:
            await asyncio.wait_for(output.written.wait(), 5)
        except TimeoutError:
            raise AssertionError(
                f"输出流 5s 内未出现 {needle!r}：{output.buffer[-400:]!r}")


async def _read_once(ch, typed: str) -> str:
    # create_app_session 让 patch_stdout 的 StdoutProxy 拿到 DummyOutput，
    # 否则在无控制台的环境下它会尝试从 sys.stdout 创建 Win32Output 而失败
    with create_app_session(output=DummyOutput()):
        with create_pipe_input() as pipe:
            ch.input._prompt = PromptSession(input=pipe, output=DummyOutput())
            pipe.send_text(typed + "\n")
            return await asyncio.wait_for(ch.input.read(), timeout=5)


def test_prompt_toolkit_input_path():
    ch = CLIChannel(MessageBus(), session_store=make_store(),
               scheduler_store=make_scheduler_store())

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
    ch = CLIChannel(MessageBus(), session_store=make_store(),
               scheduler_store=make_scheduler_store())
    ch.renderer.console = Console(file=io.StringIO(), highlight=False, width=100)

    async def scenario():
        out = RecordingOutput()
        with create_app_session(output=out):
            with create_pipe_input() as pipe:
                ch.input._prompt = PromptSession(input=pipe, output=out)
                prompt_task = asyncio.create_task(ch.input.read())
                # 等提示符真正渲染到输出流，而不是等一个固定时长
                await _wait_output(out, "you ›")
                assert ch.input.active
                ch.renderer.print_above_prompt(
                    Panel("Ace, time to drink water.",
                          title="zero · ⏰ 定时任务", border_style="yellow"),
                    prompt_active=ch.input.active)
                # run_in_terminal 是后台任务：等它的面板内容出现在流里
                panel_pos = await _wait_output(out, "Ace, time to drink water")
                # 面板打印完 ptk 会重绘提示符；等它再次出现再输入
                await _wait_output(out, "you ›", after=panel_pos + 1)
                pipe.send_text("done\n")
                text = await asyncio.wait_for(prompt_task, timeout=5)
                assert text == "done", repr(text)
                assert not ch.input.active

    asyncio.run(scenario())
