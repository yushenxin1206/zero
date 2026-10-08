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
            "last_updated_at": "2026-08-12T14:30:00",
            "title": "调试飞书消息丢失问题",
            "description": "……（maintenance session_summary 定时任务回写）"
        }
    }

Index entries are modeled by session.models.SessionMeta. Fields missing in older
index files (e.g. title, description) fall back to model defaults; unknown fields
are ignored on validation. The on-disk format stays snake_case JSON.

Session-level files inside {session_dir}:
    messages.jsonl  append-only 原始消息
    compact.json    上下文压缩记录（CompactRecord）
    summary.json    会话复盘（SessionSummary，maintenance session_summary 写入）
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from zero.models import Message, MessageBody, MessageTool, CompactRecord
from zero.session.models import SessionMeta, SessionSummary
from zero.config import ZERO_ROOT

logger = logging.getLogger(__name__)

SESSIONS_ROOT = ZERO_ROOT / "sessions"

# 自动标题（首条用户消息截断）的最大长度
TITLE_MAX_LEN = 30


# ---------------------------------------------------------------------------
# 压缩记录合成（纯函数：磁盘路径与内存快照路径共用）
# ---------------------------------------------------------------------------

def compact_message(
        rec: CompactRecord, session_id: str, channel: str) -> Message:
    """合成承载压缩记录的 user 消息（metadata.kind == "compact"）。"""
    return Message(
        session_id=session_id,
        channel=channel,
        sender="user",
        recipient="assistant",
        body=MessageBody(content=rec.content),
        metadata={"kind": "compact"},
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
        return SESSIONS_ROOT / channel / "index.json"

    @staticmethod
    def _load_index(channel: str) -> dict[str, SessionMeta]:
        path = SessionStore._index_path(channel)
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        return {sid: SessionMeta.model_validate(e) for sid, e in raw.items()}

    @staticmethod
    def _save_index(channel: str, index: dict[str, SessionMeta]) -> None:
        path = SessionStore._index_path(channel)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {sid: e.model_dump(mode="json") for sid, e in index.items()}
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @staticmethod
    def _find_entry_by_session_id(channel: str, session_id: str) -> SessionMeta | None:
        index = SessionStore._load_index(channel)
        for entry in index.values():
            if entry.session_id == session_id:
                return entry
        return None

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    @staticmethod
    def session_dir(channel: str, session_id: str) -> Path:
        """会话数据目录（messages.jsonl / compact.json / summary.json 所在处）。

        会话创建日期由 index 反查；不在 index 中（如创建时 source_id 为空、
        或 subagent 等内存会话）时退化为按今天拼路径。
        """
        entry = SessionStore._find_entry_by_session_id(channel, session_id)
        if entry is not None:
            date = entry.date
        else:
            date = datetime.now().strftime("%Y-%m-%d")
        return SESSIONS_ROOT / channel / date / session_id

    @staticmethod
    def _session_file(channel: str, session_id: str) -> Path:
        return SessionStore.session_dir(channel, session_id) / "messages.jsonl"

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
    def resolve_session(channel: str, source_id: str) -> str:
        """
        Look up or create a session for the given channel-specific source_id.

        Returns the session_id (uuid4). A new session is created on first
        contact; subsequent calls with the same source_id return the existing
        session_id.
        """
        if not source_id:
            return str(uuid4())

        index = SessionStore._load_index(channel)
        if source_id in index:
            entry = index[source_id]
            entry.last_updated_at = datetime.now()
            SessionStore._save_index(channel, index)
            return entry.session_id

        meta = SessionMeta(date=datetime.now().strftime("%Y-%m-%d"))
        index[source_id] = meta
        SessionStore._save_index(channel, index)
        return meta.session_id

    @staticmethod
    def create_session(channel: str, source_id: str) -> str:
        """
        Force-create a NEW session for source_id, replacing any existing
        binding. Use for explicit "new conversation" actions (CLI /new).
        """
        index = SessionStore._load_index(channel)
        meta = SessionMeta(date=datetime.now().strftime("%Y-%m-%d"))
        index[source_id] = meta
        SessionStore._save_index(channel, index)
        return meta.session_id

    @staticmethod
    def list_sessions(channel: str) -> list[SessionMeta]:
        """
        List all sessions of a channel, most recently updated first.

        Each returned SessionMeta has source_id filled in from the index key.
        """
        index = SessionStore._load_index(channel)
        entries = []
        for sid, entry in index.items():
            entry.source_id = sid
            entries.append(entry)
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
        """Manually set/rename a session title. Returns True if updated."""
        index = SessionStore._load_index(channel)
        for entry in index.values():
            if entry.session_id == session_id:
                entry.title = title.strip()
                SessionStore._save_index(channel, index)
                return True
        return False

    @staticmethod
    def get_model(channel: str, session_id: str) -> str:
        """Return the session's bound model (config.models key), or "" if
        the session follows the global default."""
        entry = SessionStore._find_entry_by_session_id(channel, session_id)
        return entry.model if entry else ""

    @staticmethod
    def set_model(channel: str, session_id: str, model: str) -> bool:
        """Bind a model to the session. Returns True if updated."""
        index = SessionStore._load_index(channel)
        for entry in index.values():
            if entry.session_id == session_id:
                entry.model = model.strip()
                SessionStore._save_index(channel, index)
                return True
        return False

    @staticmethod
    def auto_title(channel: str, session_id: str, text: str) -> None:
        """Set the title from the first user message, unless one is already
        set (auto or manual). No-op for sessions not in the index."""
        title = SessionStore.make_title(text)
        if not title:
            return
        index = SessionStore._load_index(channel)
        for entry in index.values():
            if entry.session_id == session_id:
                if not entry.title:
                    entry.title = title
                    SessionStore._save_index(channel, index)
                return

    @staticmethod
    def first_user_message(channel: str, session_id: str) -> str:
        """Return the content of the session's first user message ("" if
        none). Stops at the first match without loading the whole file."""
        try:
            path = SessionStore._session_file(channel, session_id)
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
    def has_messages(channel: str, session_id: str) -> bool:
        """True if the session has a non-empty messages file."""
        try:
            path = SessionStore._session_file(channel, session_id)
        except ValueError:
            return False
        return path.exists() and path.stat().st_size > 0

    @staticmethod
    def append(message: Message) -> None:
        """Append a Message to its session file.

        不触碰 index.json：turn 内每条中间消息都 touch 会让整个索引被反复
        读改写（随渠道会话数放大的写放大）。活跃时间戳由调用方在 turn 边界
        用 touch() 统一刷新一次。
        """
        path = SessionStore._session_file(message.channel, message.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(message.model_dump_json() + "\n")

    @staticmethod
    def touch(channel: str, session_id: str) -> None:
        """刷新会话的 last_updated_at（turn 边界调用一次即可）。

        last_updated_at 只服务列表排序与 maintenance 的 24h 活跃过滤，
        per-turn 粒度足够。
        """
        index = SessionStore._load_index(channel)
        for entry in index.values():
            if entry.session_id == session_id:
                entry.last_updated_at = datetime.now()
                SessionStore._save_index(channel, index)
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

    @staticmethod
    def _placeholder_tool_msg(tool_call_id: str, session_id: str,
                              channel: str) -> MessageTool:
        """孤儿 tool_call 的占位 tool 结果。

        加载时兜底修复无法得知原始原因（异常退出、进程被杀、旧版本
        bug 皆有可能），文案必须中性——不能谎称「用户取消」，否则误导
        LLM 后续行为与事后排查。具体修复动作记在日志 warning 里。
        """
        return MessageTool(
            id=str(uuid4()),
            session_id=session_id,
            channel=channel,
            sender="tool",
            recipient="assistant",
            body=MessageBody(
                content='{"error": "missing tool result '
                        '(auto-repaired at load)"}',
                tool_call_id=tool_call_id,
            ),
        )

    @staticmethod
    def repair_orphan_tool_calls(messages: list[Message], channel: str,
                                 session_id: str) -> list[Message]:
        """检测并修复孤儿 tool_calls，返回修复后的消息列表。

        孤儿 = assistant(tool_calls) 的某个 tool_call_id 没有后续配对的
        tool 消息（序列在 tool 结果齐备前被 user/assistant 消息打断，
        或文件直接结束）。残缺序列发给 LLM 会 400。

        修复 = 在断裂点插入占位 tool 结果并重写 messages.jsonl。
        无孤儿时零写盘。幂等：修复后的历史再跑一遍不会有新发现。
        """
        repaired: list[Message] = []
        pending: dict[str, str] = {}   # tool_call_id -> tool_call_id
        changed = False

        def settle():
            """结算当前 pending：每个未配对的 tool_call 补一条占位。"""
            nonlocal pending, changed
            for tc_id in pending.values():
                repaired.append(SessionStore._placeholder_tool_msg(
                    tc_id, session_id, channel))
                changed = True
            pending = {}

        for m in messages:
            tool_calls = m.body.tool_calls or []
            if m.sender == "assistant" and tool_calls:
                # 新的 tool_calls 轮次开始：上一轮若还有未配对的即孤儿
                settle()
                repaired.append(m)
                pending = {tc.id: tc.id for tc in tool_calls}
            elif m.sender == "tool" and m.body.tool_call_id:
                pending.pop(m.body.tool_call_id, None)
                repaired.append(m)
            else:
                # user / 普通 assistant / 其它：tool 结果序列至此必须闭合
                settle()
                repaired.append(m)
        settle()  # 尾部孤儿

        if changed:
            path = SessionStore._session_file(channel, session_id)
            tmp = path.with_suffix(".jsonl.tmp")
            tmp.write_text(
                "".join(m.model_dump_json() + "\n" for m in repaired),
                encoding="utf-8")
            tmp.replace(path)
            logger.warning(
                "session %s/%s: repaired orphan tool_calls "
                "(placeholder tool results inserted)", channel, session_id)
        return repaired

    @staticmethod
    def load_history(
            session_id: str,
            channel: str,
            include_compact: bool = True,
    ) -> list[Message]:
        """Load all messages for a session in chronological order.

        When include_compact is True and a compression record exists, the
        covered prefix is replaced by a single synthetic compact user message
        (metadata.kind == "compact"), keeping the tail (messages after
        covered_upto) intact. Falls back to full history if the covered
        message id can't be located.
        """
        try:
            path = SessionStore._session_file(channel, session_id)
        except ValueError:
            return []
        if not path.exists():
            return []
        messages = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                messages.append(Message.model_validate_json(line))
        # 兜底修复孤儿 tool_calls（含尾部孤儿）：任何来源的历史残缺
        # （异常退出、进程被杀、旧版本 bug）在加载时一次治愈，避免
        # 残缺序列发给 LLM 触发 400。无孤儿时零写盘。
        messages = SessionStore.repair_orphan_tool_calls(
            messages, channel, session_id)
        if not include_compact:
            return messages
        return apply_compact(
            messages, SessionStore.load_compact(channel, session_id),
            session_id, channel)

    # ------------------------------------------------------------------
    # Maintenance APIs（session_summary/memory 定时任务专用，纯存储无业务）
    # ------------------------------------------------------------------

    @staticmethod
    def list_channels() -> list[str]:
        """List all channel names that have session directories on disk."""
        if not SESSIONS_ROOT.exists():
            return []
        return sorted(p.name for p in SESSIONS_ROOT.iterdir() if p.is_dir())

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

        绝不触碰 last_updated_at——否则复盘回写会让会话在维护任务的
        "24h 活跃"过滤中永远自激（复盘→更新索引→看起来活跃→再次复盘）。
        Returns True if the session was found and updated.
        """
        index = SessionStore._load_index(channel)
        for entry in index.values():
            if entry.session_id == session_id:
                if title is not None:
                    entry.title = title.strip()
                if description is not None:
                    entry.description = description.strip()
                SessionStore._save_index(channel, index)
                return True
        return False

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
        if not SESSIONS_ROOT.exists():
            return 0
        for channel_dir in SESSIONS_ROOT.iterdir():
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
