"""P1-2：索引写入不随历史规模增长，活跃时间由消息文件推导。"""
import json
import time
from unittest.mock import patch

import pytest

from zero.bus.queue import MessageBus
from zero.channels.service import ChannelService
from zero.models import MessageBody, MessageIn, MessageOut
from zero.permission import envelope
from zero.session.store import SessionStore
from tests.support import (
    make_data_paths, make_permission, make_scheduler_store, make_store)


def test_working_dir_roundtrip_without_rewrite():
    """会话工作目录可读写；值未变化时不重写索引。"""
    channel = "cli"
    store = make_store()
    sid = store.create_session(channel, "src-working-dir")
    assert store.get_working_dir(channel, sid) == ""

    assert store.set_working_dir(channel, sid, "/tmp/zero-project") is True
    assert store.get_working_dir(channel, sid) == "/tmp/zero-project"

    index_path = store._index_path(channel)
    before = index_path.stat().st_mtime_ns
    assert store.set_working_dir(channel, sid, "/tmp/zero-project") is True
    assert index_path.stat().st_mtime_ns == before, "值未变不应重写索引"

    raw = json.loads(index_path.read_text(encoding="utf-8"))
    assert raw["src-working-dir"]["working_dir"] == "/tmp/zero-project"

    assert store.set_working_dir(channel, "missing-session", "/tmp/x") is False


def test_channel_service_sets_working_dir(tmp_path):
    """用户入口：设置/清除走校验，并写入当前会话的索引条目。"""
    store = make_store()
    service = ChannelService(
        "web", msg_bus=MessageBus(), session_store=store,
        scheduler_store=make_scheduler_store(), data_paths=make_data_paths(),
        permission=make_permission())
    source_id = "src-service-cwd"
    sid = store.create_session("web", source_id)
    service.bind(source_id, sid)

    # 数据根是 tmp_path/.zero；工作目录必须是它的兄弟目录，不能互相包含
    workspace = tmp_path.parent / f"{tmp_path.name}-project"
    workspace.mkdir()

    assert service.working_dir() == ""
    assert service.set_working_dir(str(workspace)) == str(envelope._real(workspace))
    assert service.working_dir() == str(envelope._real(workspace))

    with pytest.raises(ValueError):
        service.set_working_dir("relative/path")

    assert service.set_working_dir("") == ""
    assert service.working_dir() == ""


def test_index_is_not_rewritten_per_turn():
    """追加/心跳/自动标题/解析会话都不改写索引，写量不随历史增长。"""
    channel = "cli"
    source_id = "src-index"
    sid = make_store().create_session(channel, source_id)
    make_store().auto_title(channel, sid, "索引写入测试")

    index_path = make_store()._index_path(channel)
    before_bytes = index_path.read_text(encoding="utf-8")
    before_mtime = index_path.stat().st_mtime_ns

    calls = 0
    real_save = make_store()._save_index

    def counting_save(ch, index):
        nonlocal calls
        calls += 1
        real_save(ch, index)

    with patch.object(SessionStore, "_save_index", counting_save):
        for i in range(80):
            make_store().append(MessageIn(
                channel=channel, session_id=sid,
                body=MessageBody(content="u" * i)))
            make_store().append(MessageOut(
                channel=channel, session_id=sid,
                body=MessageBody(content="a" * i)))
            make_store().touch(channel, sid)
            make_store().auto_title(channel, sid, f"第 {i} 条")
            make_store().resolve_session(channel, source_id)

    assert calls == 0, f"每轮仍在写索引：{calls} 次"
    assert index_path.read_text(encoding="utf-8") == before_bytes
    assert index_path.stat().st_mtime_ns == before_mtime


def test_list_activity_follows_messages_mtime():
    """列表排序/维护过滤使用的活跃时间来自 messages.jsonl 的 mtime。"""
    older = make_store().create_session("cli", "src-old")
    newer = make_store().create_session("cli", "src-new")
    make_store().append(MessageIn(channel="cli", session_id=older,
                                  body=MessageBody(content="old")))
    make_store().append(MessageIn(channel="cli", session_id=newer,
                                  body=MessageBody(content="new")))
    time.sleep(0.05)
    # 给更早创建的会话追加一条消息：它应重新排到最前
    make_store().append(MessageIn(channel="cli", session_id=older,
                                  body=MessageBody(content="bump")))

    entries = make_store().list_sessions("cli")
    assert entries[0].session_id == older
    # 索引里的旧时间戳不参与排序：活跃时间必须晚于消息文件的创建时刻
    assert entries[0].last_updated_at > entries[1].last_updated_at


def test_load_history_does_not_write_index():
    """历史加载（含轮次修复）是只读索引路径。"""
    sid = make_store().create_session("cli", "src-history")
    make_store().begin_turn("cli", sid, "t1")
    make_store().append(MessageIn(channel="cli", session_id=sid,
                                  body=MessageBody(content="hi")))
    make_store().end_turn("cli", sid, "t1")

    index_path = make_store()._index_path("cli")
    before_mtime = index_path.stat().st_mtime_ns
    make_store().load_history(sid, "cli", repair_closed_turns=True,
                              active_turn_id="")
    assert index_path.stat().st_mtime_ns == before_mtime


def _snapshot(channel: str = "cli"):
    path = make_store()._index_path(channel)
    return path.read_text(encoding="utf-8"), path.stat().st_mtime_ns


def _count_writes():
    """上下文管理器：统计 _save_index 实际被调用的次数。"""
    class Counter:
        calls = 0

        def __enter__(self):
            self.real = SessionStore._save_index
            counter = self

            def counting(store_self, ch, index):
                counter.calls += 1
                counter.real(store_self, ch, index)

            self._patch = patch.object(
                SessionStore, "_save_index", counting)
            self._patch.start()
            return self

        def __exit__(self, *exc):
            self._patch.stop()

    return Counter()


def test_set_same_title_does_not_rewrite_index():
    sid = make_store().create_session("cli", "src-same-title")
    make_store().set_title("cli", sid, "标题 A")
    before = _snapshot()

    with _count_writes() as c:
        # 只差首尾空白，规范化后与磁盘值相同
        assert make_store().set_title("cli", sid, " 标题 A ") is True

    assert c.calls == 0
    assert _snapshot() == before


def test_update_meta_with_same_values_does_not_rewrite_index():
    sid = make_store().create_session("cli", "src-same-meta")
    make_store().update_meta_by_summary("cli", sid, "标题", "描述")
    before = _snapshot()

    with _count_writes() as c:
        assert make_store().update_meta_by_summary(
            "cli", sid, "标题", "描述") is True

    assert c.calls == 0
    assert _snapshot() == before


def test_update_meta_with_none_keeps_values_and_skips_write():
    sid = make_store().create_session("cli", "src-none-meta")
    make_store().update_meta_by_summary("cli", sid, "原标题", "原描述")
    before = _snapshot()

    with _count_writes() as c:
        assert make_store().update_meta_by_summary("cli", sid) is True

    assert c.calls == 0
    assert _snapshot() == before
    assert make_store().get_title("cli", sid) == "原标题"
    entry = json.loads(before[0])["src-none-meta"]
    assert entry["description"] == "原描述"


def test_real_meta_changes_still_write():
    sid = make_store().create_session("cli", "src-real-meta")
    make_store().update_meta_by_summary("cli", sid, "旧标题", "旧描述")
    before = _snapshot()

    with _count_writes() as c:
        assert make_store().update_meta_by_summary(
            "cli", sid, "新标题", None) is True

    assert c.calls == 1
    after_text, _ = _snapshot()
    assert after_text != before[0]
    entry = json.loads(after_text)["src-real-meta"]
    assert entry["title"] == "新标题"
    # None 不覆盖已有描述
    assert entry["description"] == "旧描述"
