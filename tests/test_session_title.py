"""Offline tests for session titles (no real LLM, isolated SESSIONS_ROOT).

Run: python tests/test_session_title.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import zero.session.store as store_mod
from zero.models import MessageBody, MessageIn, MessageOut
from zero.session.store import TITLE_MAX_LEN, SessionStore

# Isolate storage: redirect SESSIONS_ROOT to a temp dir for this test run.
_tmp = tempfile.TemporaryDirectory()
store_mod.SESSIONS_ROOT = Path(_tmp.name)

CHANNEL = "cli"


def fresh_session(source_id: str) -> str:
    return SessionStore.create_session(CHANNEL, source_id)


# --- make_title: whitespace collapse + truncation ---------------------------
assert SessionStore.make_title("") == ""
assert SessionStore.make_title("   \n  ") == ""
assert SessionStore.make_title("你好\n世界\t  加油") == "你好 世界 加油"
long_text = "a" * (TITLE_MAX_LEN + 10)
t = SessionStore.make_title(long_text)
assert t == "a" * TITLE_MAX_LEN + "…" and len(t) == TITLE_MAX_LEN + 1
print("make_title ok")

# --- new sessions start with an empty title ---------------------------------
sid = fresh_session("src-1")
assert SessionStore.get_title(CHANNEL, sid) == ""
entry = SessionStore.list_sessions(CHANNEL)[0]
assert entry.title == "" and entry.source_id == "src-1"
print("create_session default title ok")

# --- auto_title from first user message, no overwrite -----------------------
SessionStore.auto_title(CHANNEL, sid, "帮我看看 飞书消息 为什么丢了")
assert SessionStore.get_title(CHANNEL, sid) == "帮我看看 飞书消息 为什么丢了"
# second message must not overwrite
SessionStore.auto_title(CHANNEL, sid, "第二条消息")
assert SessionStore.get_title(CHANNEL, sid) == "帮我看看 飞书消息 为什么丢了"
# manual rename wins over later auto_title
assert SessionStore.set_title(CHANNEL, sid, "飞书排障")
SessionStore.auto_title(CHANNEL, sid, "别的内容")
assert SessionStore.get_title(CHANNEL, sid) == "飞书排障"
print("auto_title ok")

# --- set_title return values -------------------------------------------------
assert SessionStore.set_title(CHANNEL, "nonexistent-session", "x") is False
print("set_title ok")

# --- backward compat: index entries without the title field ------------------
sid2 = fresh_session("src-2")
index_path = store_mod.SESSIONS_ROOT / CHANNEL / "index.json"
index = json.loads(index_path.read_text(encoding="utf-8"))
del index["src-2"]["title"]  # simulate a pre-title entry
index["src-2"]["future_field"] = 123  # unknown fields must be ignored
index_path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
assert SessionStore.get_title(CHANNEL, sid2) == ""
legacy = [s for s in SessionStore.list_sessions(CHANNEL) if s.session_id == sid2][0]
assert legacy.title == ""
# auto_title still fills it in
SessionStore.auto_title(CHANNEL, sid2, "老会话的第一条消息")
assert SessionStore.get_title(CHANNEL, sid2) == "老会话的第一条消息"
print("backward compat ok")

# --- SessionMeta: on-disk format stays snake_case JSON, no source_id ---------
raw = json.loads(index_path.read_text(encoding="utf-8"))
for e in raw.values():
    assert set(e) >= {"session_id", "date", "last_updated_at", "title"}, e
    assert "source_id" not in e and "sourceId" not in e, e
    assert isinstance(e["last_updated_at"], str)  # ISO string on disk
print("SessionMeta serialization ok")

# --- first_user_message -------------------------------------------------------
sid3 = fresh_session("src-3")
assert SessionStore.first_user_message(CHANNEL, sid3) == ""
SessionStore.append(MessageIn(channel=CHANNEL, session_id=sid3,
                              body=MessageBody(content="第一条用户消息")))
SessionStore.append(MessageOut(channel=CHANNEL, session_id=sid3,
                               body=MessageBody(content="助手回复")))
SessionStore.append(MessageIn(channel=CHANNEL, session_id=sid3,
                              body=MessageBody(content="第二条用户消息")))
assert SessionStore.first_user_message(CHANNEL, sid3) == "第一条用户消息"
print("first_user_message ok")

print("ALL TITLE TESTS PASSED")
