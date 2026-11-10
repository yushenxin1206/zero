"""
Session storage module.

Persists conversation messages as JSONL files:
    ~/.zero/sessions/{channel}/{date}/{session_id}/messages.jsonl

Channel-level index for session lookup:
    ~/.zero/sessions/{channel}/index.json
    {
        "<source_id>": {
            "session_id": "<uuid4>",
            "date": "2026-08-12",
            "title": "调试飞书消息丢失问题",
            "description": "……（maintenance session_summary 定时任务回写）"
        }
    }

Index entries are modeled by session.models.SessionMeta. Fields missing in older
index files (e.g. title, description) fall back to model defaults; unknown fields
are ignored on validation. The on-disk format stays snake_case JSON.

The index is metadata-only: it is written when a session is created or its
title/description/model changes, never per turn. Activity time used for
listing and the "24h active" maintenance filter is derived from the session's
messages.jsonl mtime and never persisted, so a turn just appends and never
rewrites the whole index. Index reads are served from an in-process cache
invalidated by the index file's mtime; the cache also keeps a
session_id → entry map, because the index itself is keyed by source_id.

Session-level files inside {session_dir}:
    messages.jsonl  append-only 原始消息
    compact.json    上下文压缩记录（CompactRecord）
    summary.json    会话复盘（SessionSummary，maintenance session_summary 写入）
"""
import json
import logging
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator, TypeVar
from uuid import uuid4

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows 无 fcntl，退化为仅进程内锁
    fcntl = None

from zero.models import Message, MessageBody, MessageTool, CompactRecord
from zero.paths import current
from zero.session.models import SessionMeta, SessionSummary, TurnMarker

logger = logging.getLogger(__name__)

# 自动标题（首条用户消息截断）的最大长度
TITLE_MAX_LEN = 30

# 渠道索引的进程内缓存：index 文件路径 ->
# (mtime_ns, {source_id: SessionMeta}, {session_id: SessionMeta})。
# 两个映射共享同一批条目，读方只读；需要修改的调用方走 _load_index() 拿副本。
# 反向映射存在的原因：索引以 source_id 为 key，而绝大多数调用只持有 session_id。
_index_cache: dict[str, tuple[int, dict[str, SessionMeta],
                               dict[str, SessionMeta]]] = {}
_index_lock = threading.Lock()

# 每个渠道一把进程内写锁：索引的读-改-写必须整段串行，否则后台复盘回写
# 与用户改标题同时发生时会互相覆盖（写盘本身是整份重写）。
_channel_locks: dict[str, threading.Lock] = {}
_channel_locks_guard = threading.Lock()

T = TypeVar("T")


def _channel_write_lock(channel: str) -> threading.Lock:
    with _channel_locks_guard:
        lock = _channel_locks.get(channel)
        if lock is None:
            lock = _channel_locks[channel] = threading.Lock()
        return lock


@contextmanager
def _index_file_lock(channel: str) -> Iterator[None]:
    """跨进程互斥：索引同目录下的锁文件，flock 独占。

    平台没有 fcntl（Windows）时只保留进程内锁，单进程多线程仍安全。
    """
    path = SessionStore._index_path(channel).with_name(".index.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as fh:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# 压缩记录合成（纯函数：磁盘路径与内存快照路径共用）
# ---------------------------------------------------------------------------

def compact_message(
        rec: CompactRecord, session_id: str, channel: str) -> Message:
    """合成承载压缩记录的 user 消息（compact=True）。"""
    return Message(
        session_id=session_id,
        channel=channel,
        sender="user",
        recipient="assistant",
        body=MessageBody(content=rec.content),
        compact=True,
    )


def apply_compact(
        raw: list[Message],
        rec: CompactRecord | None,
        session_id: str,
        channel: str,
) -> list[Message]:
    """把压缩记录套用到全量历史上，返回 [压缩消息] + 尾部。

    covered_upto 在 raw 中定位失败时退化为全量历史（不计压缩记录），
    与 load_history 的容错语义一致；rec 为 None 时返回全量副本。
    """
    if rec is None:
        return list(raw)
    for i, m in enumerate(raw):
        if m.id == rec.covered_upto:
            return [compact_message(rec, session_id, channel)] + list(raw[i + 1:])
    return list(raw)


class SessionStore:
    """Append-only JSONL storage for conversation sessions."""

    # ------------------------------------------------------------------
    # Index file helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _index_path(channel: str) -> Path:
        return current().sessions / channel / "index.json"

    @staticmethod
    def _cached_maps(
            channel: str,
    ) -> tuple[dict[str, SessionMeta], dict[str, SessionMeta]]:
        """索引缓存：返回 (source_id→entry, session_id→entry)，按文件 mtime 失效。

        两个映射与缓存共享条目，调用方不得修改；需要改字段走 _load_index()。
        """
        path = SessionStore._index_path(channel)
        key = str(path)
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            stamp = -1
        with _index_lock:
            cached = _index_cache.get(key)
            if cached is not None and cached[0] == stamp:
                return cached[1], cached[2]
        if stamp < 0:
            by_source: dict[str, SessionMeta] = {}
        else:
            with path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
            by_source = {sid: SessionMeta.model_validate(e) for sid, e in raw.items()}
        by_session = {entry.session_id: entry for entry in by_source.values()}
        with _index_lock:
            _index_cache[key] = (stamp, by_source, by_session)
        return by_source, by_session

    @staticmethod
    def _cached_index(channel: str) -> dict[str, SessionMeta]:
        """索引的 source_id → 条目视图（共享只读）。"""
        return SessionStore._cached_maps(channel)[0]

    @staticmethod
    def _load_index(channel: str) -> dict[str, SessionMeta]:
        """索引的可写副本：供改字段后 _save_index 的调用方使用。"""
        return {sid: entry.model_copy()
                for sid, entry in SessionStore._cached_index(channel).items()}

    @staticmethod
    def _save_index(channel: str, index: dict[str, SessionMeta]) -> None:
        """原子写回索引并刷新进程内缓存（调用方必须已持有本渠道写锁）。"""
        path = SessionStore._index_path(channel)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {sid: e.model_dump(mode="json") for sid, e in index.items()}
        # 临时文件必须每次唯一：固定名在并发写时会互相踩，第二个 replace 报错
        fd, tmp_name = tempfile.mkstemp(
            dir=path.parent, prefix=".index-", suffix=".json.tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                # 先拼成整串再写：json.dump 逐块写会慢一个数量级
                f.write(json.dumps(data, ensure_ascii=False, indent=2))
            tmp.replace(path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        stamp = path.stat().st_mtime_ns
        with _index_lock:
            _index_cache[str(path)] = (
                stamp, dict(index),
                {e.session_id: e for e in index.values()})

    @staticmethod
    def _find_entry_by_session_id(channel: str, session_id: str) -> SessionMeta | None:
        return SessionStore._cached_maps(channel)[1].get(session_id)

    @staticmethod
    def _last_activity_at(channel: str, entry: SessionMeta) -> datetime:
        """会话最后活跃时间：messages.jsonl 的 mtime；从未落消息则视为未活跃。

        索引不持久化活跃时间，列表排序与 24 小时过滤都读这个派生值。返回
        epoch 表示「从未活跃」：列表里排在最后，维护任务也不会选中它。
        """
        if not entry.date:
            return datetime.fromtimestamp(0)
        path = (current().sessions / channel / entry.date
                / entry.session_id / "messages.jsonl")
        try:
            return datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            return datetime.fromtimestamp(0)

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    @staticmethod
    def session_dir(channel: str, session_id: str,
                    entry: SessionMeta | None = None) -> Path:
        """会话数据目录（messages.jsonl / compact.json / summary.json 所在处）。

        会话创建日期由 index 反查；不在 index 中（如创建时 source_id 为空、
        或 subagent 等内存会话）时退化为按今天拼路径。``entry`` 供已经持有
        索引条目的调用方直接传入，省掉一次 session_id → 条目的查表。
        """
        if entry is None:
            entry = SessionStore._find_entry_by_session_id(channel, session_id)
        date = entry.date if entry is not None else datetime.now().strftime("%Y-%m-%d")
        return current().sessions / channel / date / session_id

    @staticmethod
    def _session_file(channel: str, session_id: str,
                      entry: SessionMeta | None = None) -> Path:
        return SessionStore.session_dir(channel, session_id, entry) / "messages.jsonl"

    @staticmethod
    def _compact_file(channel: str, session_id: str) -> Path:
        return SessionStore.session_dir(channel, session_id) / "compact.json"

    @staticmethod
    def _summary_file(channel: str, session_id: str) -> Path:
        return SessionStore.session_dir(channel, session_id) / "summary.json"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def _write_index(
            channel: str,
            mutate: Callable[[dict[str, SessionMeta]], tuple[T, bool]],
    ) -> T:
        """索引的事务性读-改-写：锁内取最新副本，改完原子写回。

        锁顺序固定为「渠道写锁 → 索引锁文件 → 缓存锁」，缓存锁只出现在
        _load_index / _save_index 内部；读路径只取缓存锁，不会反向拿写锁，
        因此不存在死锁环。

        mutate 返回 (结果, 是否落盘)。第二个值为 False 表示没有实际变化
        （目标会话不存在，或改完的值与原来相同），此时不写文件、不动 mtime、
        也不刷新缓存。
        """
        with _channel_write_lock(channel), _index_file_lock(channel):
            index = SessionStore._load_index(channel)
            result, changed = mutate(index)
            if changed:
                SessionStore._save_index(channel, index)
            return result

    @staticmethod
    def resolve_session(channel: str, source_id: str) -> str:
        """
        Look up or create a session for the given channel-specific source_id.

        Returns the session_id (uuid4). A new session is created on first
        contact; subsequent calls with the same source_id return the existing
        session_id.
        """
        if not source_id:
            return str(uuid4())

        index = SessionStore._cached_index(channel)
        if source_id in index:
            # 已有会话：活跃时间由 messages.jsonl 的 mtime 表达，无需写索引
            return index[source_id].session_id

        def mutate(entries: dict[str, SessionMeta]) -> tuple[str, bool]:
            existing = entries.get(source_id)
            if existing is not None:
                # 并发下另一个写者刚建了同一 source_id：复用它，不覆盖
                return existing.session_id, False
            meta = SessionMeta(date=datetime.now().strftime("%Y-%m-%d"))
            entries[source_id] = meta
            return meta.session_id, True

        return SessionStore._write_index(channel, mutate)

    @staticmethod
    def create_session(channel: str, source_id: str) -> str:
        """
        Force-create a NEW session for source_id, replacing any existing
        binding. Use for explicit "new conversation" actions (CLI /new).
        """
        def mutate(entries: dict[str, SessionMeta]) -> tuple[str, bool]:
            meta = SessionMeta(date=datetime.now().strftime("%Y-%m-%d"))
            entries[source_id] = meta
            return meta.session_id, True

        return SessionStore._write_index(channel, mutate)

    @staticmethod
    def list_sessions(channel: str) -> list[SessionMeta]:
        """
        List all sessions of a channel, most recently updated first.

        Each returned SessionMeta has source_id filled in from the index key.
        """
        index = SessionStore._cached_index(channel)
        entries: list[SessionMeta] = []
        for sid, entry in index.items():
            # 活跃时间取自消息文件 mtime；索引条目本身不被修改（缓存共享）
            entries.append(entry.model_copy(update={
                "source_id": sid,
                "last_updated_at": SessionStore._last_activity_at(channel, entry),
            }))
        entries.sort(key=lambda e: e.last_updated_at, reverse=True)
        return entries

    # ------------------------------------------------------------------
    # Session titles
    # ------------------------------------------------------------------

    @staticmethod
    def make_title(text: str) -> str:
        """Derive a session title from a message (whitespace-collapsed,
        truncated to TITLE_MAX_LEN)."""
        title = " ".join((text or "").split())
        if len(title) > TITLE_MAX_LEN:
            title = title[:TITLE_MAX_LEN] + "…"
        return title

    @staticmethod
    def get_title(channel: str, session_id: str) -> str:
        """Return the session's title, or "" if unset/unknown."""
        entry = SessionStore._find_entry_by_session_id(channel, session_id)
        return entry.title if entry else ""

    @staticmethod
    def set_title(channel: str, session_id: str, title: str) -> bool:
        """Manually set/rename a session title. Returns True if found."""
        def mutate(entries: dict[str, SessionMeta]) -> tuple[bool, bool]:
            for entry in entries.values():
                if entry.session_id == session_id:
                    new_title = title.strip()
                    if entry.title == new_title:
                        return True, False  # 内容没变，不重写整份索引
                    entry.title = new_title
                    return True, True
            return False, False

        return SessionStore._write_index(channel, mutate)

    @staticmethod
    def get_model(channel: str, session_id: str) -> str:
        """Return the session's bound model (config.models key), or "" if
        the session follows the global default."""
        entry = SessionStore._find_entry_by_session_id(channel, session_id)
        return entry.model if entry else ""

    @staticmethod
    def set_model(channel: str, session_id: str, model: str) -> bool:
        """Bind a model to the session. Returns True if updated."""
        def mutate(entries: dict[str, SessionMeta]) -> tuple[bool, bool]:
            for entry in entries.values():
                if entry.session_id == session_id:
                    entry.model = model.strip()
                    return True, True
            return False, False

        return SessionStore._write_index(channel, mutate)

    @staticmethod
    def auto_title(channel: str, session_id: str, text: str) -> None:
        """Set the title from the first user message, unless one is already
        set (auto or manual). No-op for sessions not in the index."""
        title = SessionStore.make_title(text)
        if not title:
            return

        def mutate(entries: dict[str, SessionMeta]) -> tuple[None, bool]:
            for entry in entries.values():
                if entry.session_id == session_id:
                    if not entry.title:
                        entry.title = title
                        return None, True
                    return None, False
            return None, False

        SessionStore._write_index(channel, mutate)

    @staticmethod
    def first_user_message(channel: str, session_id: str,
                           entry: SessionMeta | None = None) -> str:
        """Return the content of the session's first user message ("" if
        none). Stops at the first match without loading the whole file."""
        try:
            path = SessionStore._session_file(channel, session_id, entry)
        except ValueError:
            return ""
        if not path.exists():
            return ""
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    m = Message.model_validate_json(line)
                except ValueError:
                    continue
                if m.sender == "user" and m.body.content:
                    return m.body.content
        return ""

    @staticmethod
    def has_messages(channel: str, session_id: str,
                     entry: SessionMeta | None = None) -> bool:
        """True if the session has a non-empty messages file.

        一次 stat 同时回答「存在」与「非空」，不再 exists 之后重复 stat。
        ``entry`` 让已经持有索引条目的调用方跳过 session_id → 条目的查表。
        """
        try:
            path = SessionStore._session_file(channel, session_id, entry)
        except ValueError:
            return False
        try:
            return path.stat().st_size > 0
        except OSError:
            return False

    @staticmethod
    def _append_json(channel: str, session_id: str, payload: str) -> None:
        path = SessionStore._session_file(channel, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(payload + "\n")

    @staticmethod
    def begin_turn(channel: str, session_id: str, turn_id: str) -> None:
        """写入轮次开始标记。必须在该轮第一条消息之前落盘。"""
        SessionStore._append_json(
            channel, session_id,
            TurnMarker(kind="turn_begin", turn_id=turn_id).model_dump_json())

    @staticmethod
    def end_turn(channel: str, session_id: str, turn_id: str,
                 status: str = "ok") -> None:
        """写入轮次结束标记。必须在该轮最后一条消息之后落盘。"""
        SessionStore._append_json(
            channel, session_id,
            TurnMarker(kind="turn_end", turn_id=turn_id,
                       status=status).model_dump_json())

    @staticmethod
    def append(message: Message) -> None:
        """Append a Message to its session file.

        不触碰 index.json：turn 内每条中间消息都 touch 会让整个索引被反复
        读改写（随渠道会话数放大的写放大）。活跃时间戳由调用方在 turn 边界
        用 touch() 统一刷新一次。
        """
        SessionStore._append_json(
            message.channel, message.session_id,
            message.model_dump_json())

    @staticmethod
    def touch(channel: str, session_id: str) -> None:
        """刷新会话最后活跃时间（turn 边界调用一次即可）。

        活跃时间只服务列表排序与 maintenance 的 24h 活跃过滤，由
        messages.jsonl 的 mtime 表达。这里只做一次 O(1) 的 utime，
        不再读改写整份 index.json——历史再长、会话再多也不放大写入。
        """
        try:
            os.utime(SessionStore._session_file(channel, session_id))
        except OSError:
            return

    @staticmethod
    def load_compact(channel: str, session_id: str) -> CompactRecord | None:
        """Load the context compression record, or None if absent/corrupt."""
        path = SessionStore._compact_file(channel, session_id)
        if not path.exists():
            return None
        try:
            return CompactRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    @staticmethod
    def save_compact(channel: str, session_id: str, record: CompactRecord) -> None:
        """Atomically persist a compression record (tmp file + replace)."""
        path = SessionStore._compact_file(channel, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(record.model_dump_json(), encoding="utf-8")
        tmp.replace(path)

    # 加载期占位 tool 结果的固定文案：既是给 LLM 的中性说明，也是
    # 加载期补写占位的固定文案。读取方不再靠文案识别，改用 synthetic 字段。
    _SYNTHETIC_NOTE = "tool result missing: turn ended before completion"

    @staticmethod
    def _placeholder_tool_msg(tool_call_id: str, session_id: str,
                              channel: str) -> MessageTool:
        """闭合轮次里缺失的 tool 结果占位。

        只在 turn_end 已经存在（写入方确定不会再补结果）时使用；文案
        保持中性，具体修复动作记在日志 warning 里。
        """
        return MessageTool(
            # 确定性 id：重复加载得到同一条占位，重写结果稳定。
            id=f"repaired-{tool_call_id}",
            session_id=session_id,
            channel=channel,
            sender="tool",
            recipient="assistant",
            body=MessageBody(
                content=json.dumps(
                    {"error": SessionStore._SYNTHETIC_NOTE},
                    ensure_ascii=False),
                tool_call_id=tool_call_id,
            ),
            synthetic=True,
        )

    @staticmethod
    def _repair_tool_sequence(
            messages: list[Message], channel: str,
            session_id: str) -> tuple[list[Message], bool]:
        """给一个已闭合的消息段补齐缺失的 tool 结果。

        只处理「已经确认不会再增长」的段（无标记的历史段，或已出现
        turn_end 的轮次）。孤儿 = assistant(tool_calls) 的某个 tool_call_id
        没有后续配对的 tool 消息；残缺序列发给 LLM 会 400。

        纯内存，不落盘；由 load_history 决定是否在安全边界写回。
        """
        repaired: list[Message] = []
        pending: dict[str, str] = {}   # tool_call_id -> tool_call_id
        changed = False

        def settle():
            nonlocal pending, changed
            for tc_id in pending.values():
                repaired.append(SessionStore._placeholder_tool_msg(
                    tc_id, session_id, channel))
                changed = True
            pending = {}

        for m in messages:
            tool_calls = m.body.tool_calls or []
            if m.sender == "assistant" and tool_calls:
                settle()
                repaired.append(m)
                pending = {tc.id: tc.id for tc in tool_calls}
            elif m.sender == "tool" and m.body.tool_call_id:
                pending.pop(m.body.tool_call_id, None)
                repaired.append(m)
            else:
                settle()
                repaired.append(m)
        settle()  # 段末仍未配对
        return repaired, changed

    @staticmethod
    def _write_records(session_id: str, channel: str,
                       records: list[Message | TurnMarker]) -> None:
        """原子重写 messages.jsonl（tmp + replace），保留轮次标记。"""
        path = SessionStore._session_file(channel, session_id)
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text(
            "".join(r.model_dump_json() + "\n" for r in records),
            encoding="utf-8")
        tmp.replace(path)

    @staticmethod
    def _read_records(path: Path) -> list[Message | TurnMarker]:
        records: list[Message | TurnMarker] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                if data.get("kind") in ("turn_begin", "turn_end"):
                    records.append(TurnMarker.model_validate(data))
                else:
                    records.append(Message.model_validate(data))
        return records

    @staticmethod
    def _group_turns(
            records: list[Message | TurnMarker],
    ) -> list[tuple[TurnMarker | None, list[Message], TurnMarker | None]]:
        """把记录切成顺序段：(begin?, messages, end?)。

        段外消息（没有 begin 的历史段）begin/end 都是 None；turn_end
        之后的零散消息归入新的无标记段，保证重写时顺序与原文一致。
        """
        groups: list[tuple[TurnMarker | None, list[Message],
                           TurnMarker | None]] = [(None, [], None)]
        for r in records:
            if isinstance(r, TurnMarker):
                if r.kind == "turn_begin":
                    # 上一段若没有 end，保留其原样；新 begin 另起一段
                    groups.append((r, [], None))
                elif r.kind == "turn_end":
                    begin, msgs, end = groups[-1]
                    if begin is not None and end is None \
                            and begin.turn_id == r.turn_id:
                        groups[-1] = (begin, msgs, r)
                        groups.append((None, [], None))
                    # 孤立 end 无对应 begin，忽略
            else:
                begin, msgs, end = groups[-1]
                groups[-1] = (begin, msgs + [r], end)
        return groups

    @staticmethod
    def load_history(
            session_id: str,
            channel: str,
            include_compact: bool = True,
            repair_closed_turns: bool = False,
            active_turn_id: str = "",
    ) -> list[Message]:
        """Load all messages for a session in chronological order.

        恢复语义由 turn_begin/turn_end 标记决定：
        - 有 end 的轮次：完整保留；若缺 tool 结果，补 synthetic 占位。
        - 无 end 的轮次：若正是调用方自己的活跃轮（active_turn_id 匹配）
          则原样返回；否则视为进程中途退出的残留，整轮排除在模型上下文
          之外，既不补占位也不写盘。
        - 没有任何标记的历史段：视为已闭合，同样只做内存补全。

        只有 repair_closed_turns=True（轮次所有者首次读历史）才把已闭合
        段的补全结果原子写回；其它读路径永远零写盘。活跃轮绝不改写。
        """
        try:
            path = SessionStore._session_file(channel, session_id)
        except ValueError:
            return []
        if not path.exists():
            return []

        groups = SessionStore._group_turns(
            SessionStore._read_records(path))
        view: list[Message] = []
        disk: list[Message | TurnMarker] = []
        changed = False

        for begin, msgs, end in groups:
            if begin is None:
                fixed, group_changed = SessionStore._repair_tool_sequence(
                    msgs, channel, session_id)
                view.extend(fixed)
                disk.extend(fixed)
                changed = changed or group_changed
                continue

            if end is None and begin.turn_id != active_turn_id:
                logger.warning(
                    "session %s/%s: dropped unfinished turn %s "
                    "(no turn_end marker)", channel, session_id, begin.turn_id)
                disk.append(begin)
                disk.extend(msgs)      # 保留残留原样，不做任何修补
                continue

            if end is None:
                # 调用方自己的飞行轮：原样参与上下文
                view.extend(msgs)
                disk.append(begin)
                disk.extend(msgs)
                continue

            fixed, group_changed = SessionStore._repair_tool_sequence(
                msgs, channel, session_id)
            view.extend(fixed)
            disk.append(begin)
            disk.extend(fixed)
            disk.append(end)
            changed = changed or group_changed

        if repair_closed_turns and changed:
            SessionStore._write_records(session_id, channel, disk)
            logger.warning(
                "session %s/%s: repaired missing tool results in closed turns",
                channel, session_id)

        if not include_compact:
            return view
        return apply_compact(
            view, SessionStore.load_compact(channel, session_id),
            session_id, channel)

    # ------------------------------------------------------------------
    # Maintenance APIs（session_summary/memory 定时任务专用，纯存储无业务）
    # ------------------------------------------------------------------

    @staticmethod
    def list_channels() -> list[str]:
        """List all channel names that have session directories on disk."""
        root = current().sessions
        if not root.exists():
            return []
        return sorted(p.name for p in root.iterdir() if p.is_dir())

    @staticmethod
    def list_recent(channel: str, since: datetime) -> list[SessionMeta]:
        """List sessions of the channel with last_updated_at >= since
        (source_id filled, most recently updated first)."""
        return [e for e in SessionStore.list_sessions(channel)
                if e.last_updated_at >= since]

    @staticmethod
    def load_summary(channel: str, session_id: str) -> SessionSummary | None:
        """Load the session summary, or None if absent/corrupt."""
        path = SessionStore._summary_file(channel, session_id)
        if not path.exists():
            return None
        try:
            return SessionSummary.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    @staticmethod
    def save_summary(channel: str, session_id: str, summary: SessionSummary) -> None:
        """Atomically persist a session summary (tmp file + replace)."""
        path = SessionStore._summary_file(channel, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(summary.model_dump_json(), encoding="utf-8")
        tmp.replace(path)

    @staticmethod
    def update_meta_by_summary(
            channel: str,
            session_id: str,
            title: str | None = None,
            description: str | None = None,
    ) -> bool:
        """Write back the index's title/description. None means "don't change".

        索引不含活跃时间：活跃时间只由 messages.jsonl 推导，所以这次回写
        不会把会话重新标成「24h 活跃」，复盘任务不会自激。
        字段值与磁盘上相同、或两个字段都没传时，不重写索引。
        Returns True if the session was found.
        """
        def mutate(entries: dict[str, SessionMeta]) -> tuple[bool, bool]:
            for entry in entries.values():
                if entry.session_id == session_id:
                    changed = False
                    if title is not None:
                        new_title = title.strip()
                        if entry.title != new_title:
                            entry.title = new_title
                            changed = True
                    if description is not None:
                        new_description = description.strip()
                        if entry.description != new_description:
                            entry.description = new_description
                            changed = True
                    return True, changed
            return False, False

        return SessionStore._write_index(channel, mutate)

    @staticmethod
    def migrate_legacy_summaries() -> int:
        """一次性迁移：旧版压缩摘要 summary.json → compact.json。

        旧结构（SummaryRecord：content + covered_upto，content 包裹
        <summary>...</summary> 标签）。能识别为旧结构且尚无 compact.json
        的会话：重命名文件，并把 content 的标签确定性改写为 <compact>；
        content 不匹配精确格式则只改名不改写并记日志。损坏的 JSON 同样
        只改名保数据。

        新版会话复盘 summary.json（SessionSummary）不会被误判：它没有
        content 字段，直接跳过。返回迁移的会话数。
        """
        migrated = 0
        root = current().sessions
        if not root.exists():
            return 0
        for channel_dir in root.iterdir():
            if not channel_dir.is_dir():
                continue
            for date_dir in channel_dir.iterdir():
                if not date_dir.is_dir():
                    continue
                for session_dir in date_dir.iterdir():
                    if not session_dir.is_dir():
                        continue
                    old = session_dir / "summary.json"
                    new = session_dir / "compact.json"
                    if not old.exists() or new.exists():
                        continue
                    try:
                        raw = json.loads(old.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        # 损坏文件：改名保留原数据，load_compact 会安全返回 None
                        old.rename(new)
                        migrated += 1
                        continue
                    if not (isinstance(raw, dict)
                            and isinstance(raw.get("content"), str)
                            and "covered_upto" in raw):
                        # 新版 SessionSummary 或无法识别的结构 → 跳过
                        continue
                    content = raw["content"]
                    if content.startswith("<summary>\n") and content.endswith("\n</summary>"):
                        raw["content"] = ("<compact>\n"
                                          + content[len("<summary>\n"):-len("\n</summary>")]
                                          + "\n</compact>")
                    else:
                        logger.warning(
                            "legacy summary content 不符合精确格式，仅改名不改写标签: %s", old)
                    new.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
                    old.unlink(missing_ok=True)
                    migrated += 1
        if migrated:
            logger.info("旧版压缩摘要迁移完成：%d 个会话 summary.json → compact.json", migrated)
        return migrated

