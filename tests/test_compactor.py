"""Offline tests for session context compression (no real LLM,
isolated storage via conftest).
"""
import asyncio
import json
from pathlib import Path

from zero.paths import current
from zero.core.compactor import (
    COMPACT_FINAL_INSTRUCTION,
    COMPRESS_PROMPT,
    Compactor,
    build_compact_messages,
    estimate_message,
    estimate_session_payload,
    estimate_tokens,
    find_tail_boundary,
)
from zero.models import (
    AgentRuntimeConfig,
    CompactConfig,
    CompactRecord,
    LLMCallOptions,
    LLMRuntime,
    Message,
    MessageBody,
    ProviderConfig,
    TokenUsage,
    ToolCall,
    ToolCallFunction,
)
from zero.session.models import SessionSummary
from zero.session.store import SessionStore

CHANNEL = "cli"


def fresh_session(source_id: str) -> str:
    return SessionStore.create_session(CHANNEL, source_id)


def make_msg(sender, content="", session_id="", tool_calls=None,
             tool_call_id=None, usage=None):
    return Message(
        session_id=session_id,
        channel=CHANNEL,
        sender=sender,
        recipient="assistant" if sender != "assistant" else "user",
        body=MessageBody(
            content=content,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            token_usage=usage,
        ),
    )


def tc(name="read", args="{}") -> ToolCall:
    return ToolCall(id="call_1", function=ToolCallFunction(name=name, arguments=args))


class FakeProvider:
    def __init__(self, chunks=None, error=False):
        self.chunks = chunks if chunks is not None else [
            ("content", "Goal: test\nProgress: nothing done")]
        self.error = error
        self.payloads = []

    async def stream_chunks(self, payload, **_kw):
        self.payloads.append(payload)
        if self.error:
            raise RuntimeError("mock failure")
        for kind, data in self.chunks:
            yield kind, data


def make_compactor(fake, window=20000, enabled=True, max_tokens=100,
                   context_compact_max=500, headroom=4096):
    rc = AgentRuntimeConfig(
        llm_call_options=LLMCallOptions(model="mock-model", max_tokens=max_tokens),
        llm_runtime=LLMRuntime(provider=ProviderConfig(),
                               context_window_tokens=window),
        reasoning_token_headroom=headroom,
    )
    # 压缩输出预算来自顶层 CompactConfig 的 context_compact_max_tokens
    # （不再取 compact 任务的 llm_call_options.max_tokens）+ compact 任务余量
    cc = AgentRuntimeConfig(
        reasoning_token_headroom=headroom,
        llm_runtime=LLMRuntime(provider=ProviderConfig(),
                               context_window_tokens=window),
        llm_call_options=LLMCallOptions(model="mock-model"))
    cs = CompactConfig(
        enabled=enabled, context_compact_max_tokens=context_compact_max)
    return Compactor(provider=fake, session_runtime=rc, compact_runtime=cc,
                     compact_config=cs)


class FakeContext:
    """maybe_compress 依赖的 Context 最小桩（避免构造真 Context 的副作用）。

    模拟 Context 的 turn 级快照接口：raw_history / compact_record 直连
    SessionStore（桩不做缓存也不影响断言，因为每个用例里 maybe_compress
    只调用一次）。
    """

    tools = None

    def __init__(self, sid, prompt="sys"):
        self.session_id = sid
        self.channel = CHANNEL
        self.system_prompt = prompt

    @property
    def raw_history(self):
        return SessionStore.load_history(
            self.session_id, self.channel, include_compact=False)

    @property
    def compact_record(self):
        return SessionStore.load_compact(self.channel, self.session_id)

    @compact_record.setter
    def compact_record(self, rec):
        pass  # 桩始终从磁盘读，回写为 no-op


def seed_big(sid):
    """两轮历史：总估算 ~15400 tokens（窗口 20000，阈值 15000）。"""
    SessionStore.append(make_msg("user", "你好" * 3500, session_id=sid))
    SessionStore.append(make_msg("assistant", "第一轮回复", session_id=sid))
    SessionStore.append(make_msg("user", "继续" * 3500, session_id=sid))
    SessionStore.append(make_msg("assistant", "", session_id=sid,
                                 tool_calls=[tc()]))
    SessionStore.append(make_msg("tool", "结果" * 10, session_id=sid,
                                 tool_call_id="call_1"))


# ---------------------------------------------------------------------------
# 估算
# ---------------------------------------------------------------------------

def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("你好") == 3          # ceil(2 * 1.1)
    assert estimate_tokens("hello") == 2         # ceil(5 * 0.3)
    assert estimate_tokens("你好hello") == 4     # ceil(2.2 + 1.5)


def test_estimate_message():
    msg = make_msg("assistant", "你好", tool_calls=[tc()])
    assert estimate_message(msg) == (
        estimate_tokens("你好") + estimate_tokens("read") + estimate_tokens("{}"))


def test_estimate_session_payload():
    raw = [make_msg("user", "你好"), make_msg("assistant", "答"),
           make_msg("user", "继续")]
    rec = CompactRecord(content="<compact>旧</compact>", covered_upto=raw[1].id)
    assert estimate_session_payload("sys", None, raw, rec) == (
        estimate_tokens("sys") + estimate_tokens(rec.content)
        + estimate_message(raw[2]))
    # tools 工具清单同样计入
    tools = [{"type": "function", "function": {"name": "read"}}]
    assert estimate_session_payload("sys", tools, raw, rec) == (
        estimate_tokens("sys")
        + estimate_tokens(json.dumps(tools, ensure_ascii=False))
        + estimate_tokens(rec.content) + estimate_message(raw[2]))
    # covered_upto 找不到 → 退化为全量、不计压缩记录
    rec_bad = CompactRecord(content="x", covered_upto="nope")
    assert estimate_session_payload("sys", None, raw, rec_bad) == (
        estimate_tokens("sys") + sum(estimate_message(m) for m in raw))


def test_find_tail_boundary():
    assert find_tail_boundary([], 100) == 0

    # 预算足够容纳全部历史 → 边界 0
    raw = [make_msg("user", "你好"), make_msg("assistant", "回答")]
    assert find_tail_boundary(raw, 1000) == 0

    # 边界落在普通 assistant 消息上，不调整（预算 300：加 c 后恰超）
    a = make_msg("assistant", "", tool_calls=[tc()])
    b = make_msg("tool", "x" * 1400, tool_call_id="call_1")  # ~420 tokens
    c = make_msg("assistant", "y" * 1000)                    # ~300 tokens
    raw = [make_msg("user", "u"), a, b, c]
    assert find_tail_boundary(raw, 300) == 3                 # 尾部=[c]，首条非 tool

    # 边界落在 tool 消息上 → 前移到其 assistant 对（预算 350：c 未超、b 超）
    assert find_tail_boundary(raw, 350) == 1

    # 连续 tool 消息：尾部起点落在 tool 上，边界继续前移
    d = make_msg("assistant", "e")
    raw = [make_msg("user", "u"), a, b, b, d]
    assert find_tail_boundary(raw, 300) == 1


def test_build_compact_messages():
    prev = CompactRecord(content="<compact>旧</compact>", covered_upto="x")
    raw = [make_msg("user", "你好"), make_msg("assistant", "你好！")]
    msgs = build_compact_messages(raw, prev)
    assert msgs[0] == {"role": "system", "content": COMPRESS_PROMPT}
    assert msgs[1]["role"] == "user" and "<previous_compact>" in msgs[1]["content"]
    assert msgs[2] == {"role": "user", "content": "你好"}
    assert msgs[3] == {"role": "assistant", "content": "你好！"}
    assert msgs[-1] == {"role": "user", "content": COMPACT_FINAL_INSTRUCTION}

    a = make_msg("assistant", "", tool_calls=[tc()])
    b = make_msg("tool", "结果", tool_call_id="call_1")
    msgs = build_compact_messages([make_msg("user", "x"), a, b], None)
    assert msgs[1] == {"role": "user", "content": "x"}
    assert msgs[2]["role"] == "assistant"
    assert msgs[2]["tool_calls"][0]["function"]["name"] == "read"
    assert msgs[3] == {"role": "tool", "tool_call_id": "call_1",
                       "content": "结果"}
    # 无 previous_compact 时同样以 user 指令收尾（防止 assistant 裸结尾续写）
    assert msgs[-1] == {"role": "user", "content": COMPACT_FINAL_INSTRUCTION}


# ---------------------------------------------------------------------------
# load_history 合成与退化
# ---------------------------------------------------------------------------

def test_load_history_synthesis():
    sid = fresh_session("src-comp")
    SessionStore.append(make_msg("user", "第一条", session_id=sid))
    m2 = make_msg("assistant", "回复", session_id=sid)
    SessionStore.append(m2)
    m3 = make_msg("user", "第二条", session_id=sid)
    SessionStore.append(m3)
    assert len(SessionStore.load_history(sid, CHANNEL)) == 3

    SessionStore.save_compact(
        CHANNEL, sid,
        CompactRecord(content="<compact>摘要</compact>", covered_upto=m2.id))
    hist = SessionStore.load_history(sid, CHANNEL)
    assert len(hist) == 2
    assert hist[0].sender == "user" and hist[0].metadata.get("kind") == "compact"
    assert "<compact>摘要</compact>" in hist[0].body.content
    assert hist[1].id == m3.id
    # include_compact=False → 全量
    assert len(SessionStore.load_history(sid, CHANNEL, include_compact=False)) == 3
    # covered_upto 找不到 → 退化全量
    SessionStore.save_compact(
        CHANNEL, sid, CompactRecord(content="x", covered_upto="不存在"))
    assert len(SessionStore.load_history(sid, CHANNEL)) == 3
    # 损坏的 compact.json → None
    SessionStore._compact_file(CHANNEL, sid).write_text("{bad json", encoding="utf-8")
    assert SessionStore.load_compact(CHANNEL, sid) is None


# ---------------------------------------------------------------------------
# maybe_compress 集成
# ---------------------------------------------------------------------------

def test_maybe_compress_over_threshold():
    fake = FakeProvider()
    comp = make_compactor(fake)
    sid = fresh_session("src-big")
    seed_big(sid)
    assert asyncio.run(comp.maybe_compress(FakeContext(sid))) is True

    rec = SessionStore.load_compact(CHANNEL, sid)
    assert rec is not None and "<compact>" in rec.content
    hist = SessionStore.load_history(sid, CHANNEL)
    assert hist[0].sender == "user" and hist[0].metadata.get("kind") == "compact"
    assert hist[1].sender != "tool"  # 尾部首条不是 tool → 无孤儿 tool
    assert len(hist) == 4  # [压缩, user, assistant(带tool_calls), tool]

    # 压缩模型 payload 校验
    assert len(fake.payloads) == 1
    payload = fake.payloads[0]
    assert payload.tools is None
    # temperature 由 compact 任务的解析结果决定（未显式写 → 默认 0.1）
    assert payload.llm_call_options.temperature == 0.1
    assert payload.llm_call_options.max_tokens == 500 + 4096
    assert payload.messages[0] == {"role": "system", "content": COMPRESS_PROMPT}


def test_maybe_compress_below_threshold():
    fake = FakeProvider()
    comp = make_compactor(fake)
    sid = fresh_session("src-small")
    SessionStore.append(make_msg("user", "你好", session_id=sid))
    SessionStore.append(make_msg("assistant", "你好！", session_id=sid))
    assert asyncio.run(comp.maybe_compress(FakeContext(sid))) is False
    assert SessionStore.load_compact(CHANNEL, sid) is None
    assert len(SessionStore.load_history(sid, CHANNEL)) == 2


def test_maybe_compress_fail_safe():
    fake = FakeProvider(error=True)
    comp = make_compactor(fake)
    sid = fresh_session("src-err")
    seed_big(sid)
    assert asyncio.run(comp.maybe_compress(FakeContext(sid))) is False
    assert SessionStore.load_compact(CHANNEL, sid) is None
    assert len(SessionStore.load_history(sid, CHANNEL)) == 5


def test_maybe_compress_disabled():
    fake = FakeProvider()
    comp = make_compactor(fake, enabled=False)
    sid = fresh_session("src-disabled")
    seed_big(sid)
    assert asyncio.run(comp.maybe_compress(FakeContext(sid))) is False
    assert SessionStore.load_compact(CHANNEL, sid) is None


def test_maybe_compress_fits_in_tail():
    """小窗口（trigger 为负）+ 历史能整体装进尾部预算 → 不压缩。"""
    fake = FakeProvider()
    comp = make_compactor(fake, window=2000)
    sid = fresh_session("src-fits")
    SessionStore.append(make_msg("user", "你好", session_id=sid))
    SessionStore.append(make_msg("assistant", "你好！", session_id=sid))
    assert asyncio.run(comp.maybe_compress(FakeContext(sid))) is False
    assert SessionStore.load_compact(CHANNEL, sid) is None


def test_maybe_compress_no_gain():
    """压缩后预估仍超阈值 → 放弃且不调 LLM。

    window=6000, max_tokens=1000 → trigger=min(4500, 904)=904；tail=1500。
    历史约 4400 超 trigger，但 post=1+500+2200=2701 ≥ 904。
    """
    fake = FakeProvider()
    comp = make_compactor(fake, window=6000, max_tokens=1000)
    sid = fresh_session("src-nowin")
    SessionStore.append(make_msg("user", "你好" * 1000, session_id=sid))
    SessionStore.append(make_msg("assistant", "答", session_id=sid))
    SessionStore.append(make_msg("user", "继续" * 1000, session_id=sid))
    assert asyncio.run(comp.maybe_compress(FakeContext(sid))) is False
    assert SessionStore.load_compact(CHANNEL, sid) is None
    assert fake.payloads == []


def test_maybe_compress_empty_result():
    """压缩流正常结束但无 content → 不落盘，历史不受损。"""
    fake = FakeProvider(chunks=[])
    comp = make_compactor(fake)
    sid = fresh_session("src-empty")
    seed_big(sid)
    assert asyncio.run(comp.maybe_compress(FakeContext(sid))) is False
    assert SessionStore.load_compact(CHANNEL, sid) is None
    assert len(SessionStore.load_history(sid, CHANNEL)) == 5


def test_maybe_compress_save_failure(monkeypatch):
    """压缩记录落盘抛 OSError（磁盘满等）→ fail-safe。"""
    def disk_full(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(SessionStore, "save_compact", staticmethod(disk_full))
    fake = FakeProvider()
    comp = make_compactor(fake)
    sid = fresh_session("src-diskfull")
    seed_big(sid)
    assert asyncio.run(comp.maybe_compress(FakeContext(sid))) is False
    assert SessionStore.load_compact(CHANNEL, sid) is None


def test_maybe_compress_truncated():
    """输出被 max_tokens 截断（usage 报满）→ 放弃，不落盘。"""
    fake = FakeProvider(chunks=[
        ("content", "# 摘要\n\n## Goal\n1. 用户（Ace）要求："),
        ("usage", TokenUsage(completion_tokens=4596, total_tokens=4596)),
    ])
    comp = make_compactor(fake)  # context_compact_max=500 → budget=500+4096=4596
    sid = fresh_session("src-truncated")
    seed_big(sid)
    assert asyncio.run(comp.maybe_compress(FakeContext(sid))) is False
    assert SessionStore.load_compact(CHANNEL, sid) is None


def test_maybe_compress_partial_keeps_old():
    """无 usage 信号但内容是残句（缺 Progress 小节）→ 旧压缩记录不被覆盖。"""
    sid = fresh_session("src-partial")
    seed_big(sid)
    assert asyncio.run(
        make_compactor(FakeProvider()).maybe_compress(FakeContext(sid))) is True
    old_rec = SessionStore.load_compact(CHANNEL, sid)
    SessionStore.append(make_msg("user", "追问" * 4000, session_id=sid))
    SessionStore.append(make_msg("assistant", "第二轮", session_id=sid))

    fake = FakeProvider(chunks=[
        ("content", "# 摘要\n\n## Goal\n1. 用户（Ace）要求：")])
    assert asyncio.run(
        make_compactor(fake).maybe_compress(FakeContext(sid))) is False
    assert SessionStore.load_compact(CHANNEL, sid).covered_upto == old_rec.covered_upto


def test_maybe_compress_chained():
    """二次压缩：旧压缩记录作为 <previous_compact> 注入，covered_upto 前移。"""
    fake = FakeProvider()
    comp = make_compactor(fake)
    sid = fresh_session("src-chain")
    seed_big(sid)
    assert asyncio.run(comp.maybe_compress(FakeContext(sid))) is True
    first_covered = SessionStore.load_compact(CHANNEL, sid).covered_upto
    SessionStore.append(make_msg("user", "追问" * 4000, session_id=sid))
    SessionStore.append(make_msg("assistant", "第二轮", session_id=sid))
    assert asyncio.run(comp.maybe_compress(FakeContext(sid))) is True

    second_msgs = fake.payloads[1].messages
    assert any(m["role"] == "user" and "<previous_compact>" in m["content"]
               for m in second_msgs)
    # 增量压缩：首次已覆盖的旧消息不再重送
    second_texts = [m.get("content") or "" for m in second_msgs]
    assert not any(t == "你好" * 3500 for t in second_texts)
    assert any(t == "继续" * 3500 for t in second_texts)
    rec = SessionStore.load_compact(CHANNEL, sid)
    assert rec.covered_upto != first_covered


# ---------------------------------------------------------------------------
# migrate_legacy_summaries
# ---------------------------------------------------------------------------

def _write_legacy_summary(sid: str, content: str, covered: str) -> Path:
    """直接在会话目录写入旧版 SummaryRecord 结构的 summary.json。"""
    path = current().sessions / CHANNEL
    for date_dir in path.iterdir():
        target = date_dir / sid / "summary.json"
        if target.parent.is_dir():
            target.write_text(
                json.dumps({"content": content, "covered_upto": covered,
                            "created_at": "2026-01-01T00:00:00"},
                           ensure_ascii=False),
                encoding="utf-8")
            return target
    raise AssertionError(f"session dir not found for {sid}")


def test_migrate_legacy_summary_exact_tags():
    sid = fresh_session("src-legacy")
    SessionStore.append(make_msg("user", "旧会话", session_id=sid))
    last = make_msg("assistant", "回复", session_id=sid)
    SessionStore.append(last)
    legacy_path = _write_legacy_summary(
        sid, "<summary>\n## Goal\n测试\n</summary>", last.id)

    assert SessionStore.migrate_legacy_summaries() >= 1
    assert not legacy_path.exists()
    migrated = SessionStore.load_compact(CHANNEL, sid)
    assert migrated is not None
    assert migrated.content == "<compact>\n## Goal\n测试\n</compact>"
    assert migrated.covered_upto == last.id
    # 迁移后 load_history 正常合成
    hist = SessionStore.load_history(sid, CHANNEL, include_compact=True)
    assert hist[0].metadata.get("kind") == "compact"
    # 二次迁移幂等
    assert SessionStore.migrate_legacy_summaries() >= 0
    assert SessionStore.load_compact(CHANNEL, sid) is not None


def test_migrate_leaves_new_style_summary():
    sid = fresh_session("src-newstyle")
    SessionStore.append(make_msg("user", "新复盘", session_id=sid))
    SessionStore.save_summary(CHANNEL, sid, SessionSummary(
        title="t", description="d", done=["a"], covered_upto="x"))
    before = SessionStore._summary_file(CHANNEL, sid).read_text(encoding="utf-8")
    SessionStore.migrate_legacy_summaries()
    after = SessionStore._summary_file(CHANNEL, sid).read_text(encoding="utf-8")
    assert before == after
    assert not SessionStore._compact_file(CHANNEL, sid).exists()


def test_migrate_inexact_tags_only_renames():
    sid = fresh_session("src-partialfmt")
    SessionStore.append(make_msg("user", "x", session_id=sid))
    _write_legacy_summary(sid, "raw text without tags", "whatever")
    SessionStore.migrate_legacy_summaries()
    rec = SessionStore.load_compact(CHANNEL, sid)
    assert rec is not None and rec.content == "raw text without tags"


def test_migrate_broken_json_only_renames():
    sid = fresh_session("src-broken")
    SessionStore.append(make_msg("user", "y", session_id=sid))
    broken = _write_legacy_summary(sid, "irrelevant", "x")
    broken.write_text("{bad json", encoding="utf-8")
    SessionStore.migrate_legacy_summaries()
    assert not broken.exists()
    assert (broken.parent / "compact.json").exists()
    assert SessionStore.load_compact(CHANNEL, sid) is None
