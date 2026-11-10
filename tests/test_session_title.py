"""Offline tests for session titles (isolated data root via conftest)."""
import json

from zero.models import MessageBody, MessageIn, MessageOut
from zero.session.store import TITLE_MAX_LEN
from tests.support import current, make_store

CHANNEL = "cli"


def fresh_session(source_id: str) -> str:
    return make_store().create_session(CHANNEL, source_id)


def index_path():
    return current().sessions / CHANNEL / "index.json"


def test_make_title():
    assert make_store().make_title("") == ""
    assert make_store().make_title("   \n  ") == ""
    assert make_store().make_title("你好\n世界\t  加油") == "你好 世界 加油"
    long_text = "a" * (TITLE_MAX_LEN + 10)
    title = make_store().make_title(long_text)
    assert title == "a" * TITLE_MAX_LEN + "…"
    assert len(title) == TITLE_MAX_LEN + 1


def test_new_session_has_empty_title():
    sid = fresh_session("src-1")
    assert make_store().get_title(CHANNEL, sid) == ""
    entry = make_store().list_sessions(CHANNEL)[0]
    assert entry.title == "" and entry.source_id == "src-1"


def test_auto_title_does_not_overwrite():
    sid = fresh_session("src-1")
    make_store().auto_title(CHANNEL, sid, "帮我看看 飞书消息 为什么丢了")
    assert make_store().get_title(CHANNEL, sid) == "帮我看看 飞书消息 为什么丢了"
    # 第二条消息不得覆盖
    make_store().auto_title(CHANNEL, sid, "第二条消息")
    assert make_store().get_title(CHANNEL, sid) == "帮我看看 飞书消息 为什么丢了"
    # 手工改名后，后续 auto_title 不得覆盖
    assert make_store().set_title(CHANNEL, sid, "飞书排障")
    make_store().auto_title(CHANNEL, sid, "别的内容")
    assert make_store().get_title(CHANNEL, sid) == "飞书排障"


def test_set_title_missing_session():
    assert make_store().set_title(CHANNEL, "nonexistent-session", "x") is False


def test_backward_compatible_index_entry():
    """缺少 title 字段的旧索引条目：默认空标题，未知字段忽略。"""
    sid2 = fresh_session("src-2")
    path = index_path()
    index = json.loads(path.read_text(encoding="utf-8"))
    del index["src-2"]["title"]
    index["src-2"]["future_field"] = 123
    path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")

    assert make_store().get_title(CHANNEL, sid2) == ""
    legacy = [s for s in make_store().list_sessions(CHANNEL)
              if s.session_id == sid2][0]
    assert legacy.title == ""
    # auto_title 仍能补上
    make_store().auto_title(CHANNEL, sid2, "老会话的第一条消息")
    assert make_store().get_title(CHANNEL, sid2) == "老会话的第一条消息"


def test_index_serialization_is_snake_case():
    """磁盘格式保持 snake_case；活跃时间与 source_id 都是运行时字段。"""
    fresh_session("src-2")
    raw = json.loads(index_path().read_text(encoding="utf-8"))
    for entry in raw.values():
        assert set(entry) >= {"session_id", "date", "title"}
        assert "source_id" not in entry and "sourceId" not in entry
        assert "last_updated_at" not in entry and "lastUpdatedAt" not in entry


def test_first_user_message():
    sid3 = fresh_session("src-3")
    assert make_store().first_user_message(CHANNEL, sid3) == ""
    make_store().append(MessageIn(channel=CHANNEL, session_id=sid3,
                                  body=MessageBody(content="第一条用户消息")))
    make_store().append(MessageOut(channel=CHANNEL, session_id=sid3,
                                   body=MessageBody(content="助手回复")))
    make_store().append(MessageIn(channel=CHANNEL, session_id=sid3,
                                  body=MessageBody(content="第二条用户消息")))
    assert make_store().first_user_message(CHANNEL, sid3) == "第一条用户消息"
