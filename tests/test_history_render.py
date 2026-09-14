"""Regression test: /history output must not contain literal backslash-u
escapes (Windows pager used to corrupt non-GBK chars like U+203A).

Run: python tests/test_history_render.py
"""
import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import session.store as store_mod
from datacls import MessageBody, MessageIn, MessageOut, MessageTool
from session.store import SessionStore

# Isolate storage: redirect SESSIONS_ROOT to a temp dir for this test run.
_tmp = tempfile.TemporaryDirectory()
store_mod.SESSIONS_ROOT = Path(_tmp.name)

from bus.queue import MessageBus
from channels.cli import CLIChannel, CHANNEL

PROMPT_CHAR = chr(0x203A)  # ›
ESCAPED = chr(92) + "u203a"  # literal backslash + u203a

sid = SessionStore.create_session(CHANNEL, "render-src")
SessionStore.append(MessageIn(channel=CHANNEL, session_id=sid,
                              body=MessageBody(content="你好，测试历史渲染")))
SessionStore.append(MessageTool(channel=CHANNEL, session_id=sid,
                                body=MessageBody(content="tool output")))
SessionStore.append(MessageOut(channel=CHANNEL, session_id=sid,
                               body=MessageBody(content="助手回复内容")))

ch = CLIChannel(MessageBus())
ch.session_id = sid
buf = io.StringIO()
from rich.console import Console
ch.console = Console(file=buf, highlight=False, width=100)

ch._print_full_history()
out = buf.getvalue()

assert ESCAPED not in out, "history output contains literal \\u203a escape"
assert f"you {PROMPT_CHAR}" in out, "user prompt line missing"
assert f"tool {PROMPT_CHAR}" in out, "tool line missing"
assert "助手回复内容" in out, "assistant reply missing"
print("history render ok: no escapes, all message kinds present")

print("ALL HISTORY RENDER TESTS PASSED")
