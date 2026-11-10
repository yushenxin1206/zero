"""Regression test: /history output must not contain literal backslash-u
escapes (Windows pager used to corrupt non-GBK chars like U+203A).
"""
import io

from rich.console import Console

from zero.bus.queue import MessageBus
from zero.channels.cli import CHANNEL, CLIChannel
from zero.models import MessageBody, MessageIn, MessageOut, MessageTool
from tests.support import make_data_paths, make_permission, make_scheduler_store, make_store

PROMPT_CHAR = chr(0x203A)  # ›
ESCAPED = chr(92) + "u203a"  # literal backslash + u203a


def test_history_render_has_no_escapes_and_filters_tools():
    sid = make_store().create_session(CHANNEL, "render-src")
    make_store().append(MessageIn(channel=CHANNEL, session_id=sid,
                                  body=MessageBody(content="你好，测试历史渲染")))
    make_store().append(MessageTool(channel=CHANNEL, session_id=sid,
                                    body=MessageBody(content="tool output")))
    make_store().append(MessageOut(channel=CHANNEL, session_id=sid,
                                   body=MessageBody(content="助手回复内容")))

    buf = io.StringIO()
    ch = CLIChannel(MessageBus(), session_store=make_store(), data_paths=make_data_paths(), permission=make_permission(),
                    scheduler_store=make_scheduler_store(), console=Console(
        file=buf, highlight=False, width=100))
    ch.service.session_id = sid

    ch.renderer.print_full_history(sid)
    out = buf.getvalue()

    assert ESCAPED not in out, "history output contains literal \\u203a escape"
    assert f"you {PROMPT_CHAR}" in out, "user prompt line missing"
    assert "tool output" not in out, "tool line should be filtered out"
    assert "tool " not in out, "tool data should not appear in history"
    assert "助手回复内容" in out, "assistant reply missing"
