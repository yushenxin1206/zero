"""P1-2：索引写入不随历史规模增长，活跃时间由消息文件推导。"""
import time
from unittest.mock import patch

from zero.models import MessageBody, MessageIn, MessageOut
from zero.session.store import SessionStore


def test_index_is_not_rewritten_per_turn():
    """追加/心跳/自动标题/解析会话都不改写索引，写量不随历史增长。"""
    channel = "cli"
    source_id = "src-index"
    sid = SessionStore.create_session(channel, source_id)
    SessionStore.auto_title(channel, sid, "索引写入测试")

    index_path = SessionStore._index_path(channel)
    before_bytes = index_path.read_text(encoding="utf-8")
    before_mtime = index_path.stat().st_mtime_ns

    calls = 0
    real_save = SessionStore._save_index

    def counting_save(ch, index):
        nonlocal calls
        calls += 1
        real_save(ch, index)

    with patch.object(SessionStore, "_save_index", counting_save):
        for i in range(80):
            SessionStore.append(MessageIn(
                channel=channel, session_id=sid,
                body=MessageBody(content="u" * i)))
            SessionStore.append(MessageOut(
                channel=channel, session_id=sid,
                body=MessageBody(content="a" * i)))
            SessionStore.touch(channel, sid)
            SessionStore.auto_title(channel, sid, f"第 {i} 条")
            SessionStore.resolve_session(channel, source_id)

    assert calls == 0, f"每轮仍在写索引：{calls} 次"
    assert index_path.read_text(encoding="utf-8") == before_bytes
    assert index_path.stat().st_mtime_ns == before_mtime


def test_list_activity_follows_messages_mtime():
    """列表排序/维护过滤使用的活跃时间来自 messages.jsonl 的 mtime。"""
    older = SessionStore.create_session("cli", "src-old")
    newer = SessionStore.create_session("cli", "src-new")
    SessionStore.append(MessageIn(channel="cli", session_id=older,
                                  body=MessageBody(content="old")))
    SessionStore.append(MessageIn(channel="cli", session_id=newer,
                                  body=MessageBody(content="new")))
    time.sleep(0.05)
    # 给更早创建的会话追加一条消息：它应重新排到最前
    SessionStore.append(MessageIn(channel="cli", session_id=older,
                                  body=MessageBody(content="bump")))

    entries = SessionStore.list_sessions("cli")
    assert entries[0].session_id == older
    # 索引里的旧时间戳不参与排序：活跃时间必须晚于消息文件的创建时刻
    assert entries[0].last_updated_at > entries[1].last_updated_at


def test_load_history_does_not_write_index():
    """历史加载（含轮次修复）是只读索引路径。"""
    sid = SessionStore.create_session("cli", "src-history")
    SessionStore.begin_turn("cli", sid, "t1")
    SessionStore.append(MessageIn(channel="cli", session_id=sid,
                                  body=MessageBody(content="hi")))
    SessionStore.end_turn("cli", sid, "t1")

    index_path = SessionStore._index_path("cli")
    before_mtime = index_path.stat().st_mtime_ns
    SessionStore.load_history(sid, "cli", repair_closed_turns=True,
                              active_turn_id="")
    assert index_path.stat().st_mtime_ns == before_mtime
