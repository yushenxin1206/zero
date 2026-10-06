"""Offline tests for session context compression (no real LLM, isolated storage).

Run: python tests/test_compactor.py
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import zero.session.store as store_mod
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
from zero.session.store import SessionStore

# Isolate storage: redirect SESSIONS_ROOT to a temp dir for this test run.
_tmp = tempfile.TemporaryDirectory()
store_mod.SESSIONS_ROOT = Path(_tmp.name)

CHANNEL = "cli"


def fresh_session(source_id: str) -> str:
    return SessionStore.create_session(CHANNEL, source_id)


def make_msg(sender, content="", session_id="", tool_calls=None, tool_call_id=None, usage=None):
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


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# --- estimate_tokens: CJK vs ASCII weighting ---------------------------------
assert estimate_tokens("") == 0
assert estimate_tokens("你好") == 3          # ceil(2 * 1.1)
assert estimate_tokens("hello") == 2         # ceil(5 * 0.3)
assert estimate_tokens("你好hello") == 4     # ceil(2.2 + 1.5)
print("estimate_tokens ok")


# --- estimate_message: content + tool_calls arguments ------------------------
m = make_msg("assistant", "你好", tool_calls=[tc()])
assert estimate_message(m) == estimate_tokens("你好") + estimate_tokens("read") + estimate_tokens("{}")
print("estimate_message ok")


# --- estimate_session_payload: raw + rec 合成（含 tools 计入）--------------------
raw = [make_msg("user", "你好"), make_msg("assistant", "答"), make_msg("user", "继续")]
rec = CompactRecord(content="<compact>旧</compact>", covered_upto=raw[1].id)
assert estimate_session_payload("sys", None, raw, rec) == (
    estimate_tokens("sys") + estimate_tokens(rec.content) + estimate_message(raw[2]))
# tools 工具清单同样计入
tools = [{"type": "function", "function": {"name": "read"}}]
assert estimate_session_payload("sys", tools, raw, rec) == (
    estimate_tokens("sys") + estimate_tokens(json.dumps(tools, ensure_ascii=False))
    + estimate_tokens(rec.content) + estimate_message(raw[2]))
# covered_upto 找不到 → 退化为全量、不计压缩记录
rec_bad = CompactRecord(content="x", covered_upto="nope")
assert estimate_session_payload("sys", None, raw, rec_bad) == (
    estimate_tokens("sys") + sum(estimate_message(m) for m in raw))
print("estimate_session_payload ok")


# --- find_tail_boundary -------------------------------------------------------
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
assert find_tail_boundary(raw, 350) == 1                 # 尾部=[a,b,c]，首条非 tool

# 连续 tool 消息：尾部起点落在 tool 上，边界继续前移
d = make_msg("assistant", "e")                           # ~0 tokens
raw = [make_msg("user", "u"), a, b, b, d]                # a=assistant(tool_calls), b=tool, b=tool, d=assistant
assert find_tail_boundary(raw, 300) == 1                 # 尾部=[a,b,b,d]，首条非 tool
print("find_tail_boundary ok")


# --- build_compact_messages --------------------------------------------------
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
assert msgs[2]["role"] == "assistant" and msgs[2]["tool_calls"][0]["function"]["name"] == "read"
assert msgs[3] == {"role": "tool", "tool_call_id": "call_1", "content": "结果"}
# 无 previous_compact 时同样以 user 指令收尾（防止 assistant 裸结尾续写）
assert msgs[-1] == {"role": "user", "content": COMPACT_FINAL_INSTRUCTION}
print("build_compact_messages ok")


# --- load_history synthesis & fallback ----------------------------------------
sid = fresh_session("src-comp")
SessionStore.append(make_msg("user", "第一条", session_id=sid))
m2 = make_msg("assistant", "回复", session_id=sid)
SessionStore.append(m2)
m3 = make_msg("user", "第二条", session_id=sid)
SessionStore.append(m3)
assert len(SessionStore.load_history(sid, CHANNEL)) == 3

SessionStore.save_compact(CHANNEL, sid, CompactRecord(content="<compact>摘要</compact>", covered_upto=m2.id))
hist = SessionStore.load_history(sid, CHANNEL)
assert len(hist) == 2
assert hist[0].sender == "user" and hist[0].metadata.get("kind") == "compact"
assert "<compact>摘要</compact>" in hist[0].body.content
assert hist[1].id == m3.id
# include_compact=False → 全量
assert len(SessionStore.load_history(sid, CHANNEL, include_compact=False)) == 3
# covered_upto 找不到 → 退化全量
SessionStore.save_compact(CHANNEL, sid, CompactRecord(content="x", covered_upto="不存在"))
assert len(SessionStore.load_history(sid, CHANNEL)) == 3
# 损坏的 compact.json → None
SessionStore._compact_file(CHANNEL, sid).write_text("{bad json", encoding="utf-8")
assert SessionStore.load_compact(CHANNEL, sid) is None
print("load_history ok")


# --- Compactor.maybe_compress integration (mocked provider) -------------------
class FakeProvider:
    def __init__(self, chunks=None, error=False):
        self.chunks = chunks if chunks is not None else [("content", "Goal: test\nProgress: nothing done")]
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
        llm_call_options=LLMCallOptions(model="mock-model"))
    cs = CompactConfig(
        enabled=enabled, context_compact_max_tokens=context_compact_max)
    return Compactor(provider=fake, session_runtime=rc, compact_runtime=cc,
                     compact_config=cs)


class FakeContext:
    """maybe_compress 依赖的 Context 最小桩（避免构造真 Context 的副作用）。

    模拟 Context 的 turn 级快照接口：raw_history / compact_record 直连
    SessionStore（真 Context 每 turn 只读一次盘；桩不做缓存也不影响断言，
    因为每个用例里 maybe_compress 只调用一次）。
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
    a = make_msg("assistant", "", session_id=sid, tool_calls=[tc()])
    SessionStore.append(a)
    SessionStore.append(make_msg("tool", "结果" * 10, session_id=sid, tool_call_id="call_1"))


# 超阈值 → 压缩成功，压缩记录落盘，合成后首条为压缩消息且尾部无孤儿 tool
fake = FakeProvider()
comp = make_compactor(fake)
sid = fresh_session("src-big")
seed_big(sid)
assert run(comp.maybe_compress(FakeContext(sid))) is True
rec = SessionStore.load_compact(CHANNEL, sid)
assert rec is not None and "<compact>" in rec.content
hist = SessionStore.load_history(sid, CHANNEL)
assert hist[0].sender == "user" and hist[0].metadata.get("kind") == "compact"
assert hist[1].sender != "tool"  # 尾部首条不是 tool → 无孤儿 tool
assert len(hist) == 4  # [压缩, user, assistant(带tool_calls), tool]
# 压缩模型 payload 校验
assert len(fake.payloads) == 1
pl = fake.payloads[0]
assert pl.tools is None
# temperature 不再强制 0：由 compact 任务的解析结果决定（compact 未显式写
# temperature，落 LLMCallOptions 默认 0.1）
assert pl.llm_call_options.temperature == 0.1
assert pl.llm_call_options.max_tokens == 500 + 4096  # context_compact_max + reasoning 余量
assert pl.messages[0] == {"role": "system", "content": COMPRESS_PROMPT}
print("maybe_compress over-threshold ok")

# 未超阈值 → 不触发
fake2 = FakeProvider()
comp2 = make_compactor(fake2)
sid2 = fresh_session("src-small")
SessionStore.append(make_msg("user", "你好", session_id=sid2))
SessionStore.append(make_msg("assistant", "你好！", session_id=sid2))
assert run(comp2.maybe_compress(FakeContext(sid2))) is False
assert SessionStore.load_compact(CHANNEL, sid2) is None
assert len(SessionStore.load_history(sid2, CHANNEL)) == 2
print("maybe_compress below-threshold ok")

# 压缩模型异常 → fail-safe，主流程不受影响
fake3 = FakeProvider(error=True)
comp3 = make_compactor(fake3)
sid3 = fresh_session("src-err")
seed_big(sid3)
assert run(comp3.maybe_compress(FakeContext(sid3))) is False
assert SessionStore.load_compact(CHANNEL, sid3) is None
assert len(SessionStore.load_history(sid3, CHANNEL)) == 5
print("maybe_compress fail-safe ok")

# 关闭压缩 → 永不触发
fake4 = FakeProvider()
comp4 = make_compactor(fake4, enabled=False)
sid4 = fresh_session("src-disabled")
seed_big(sid4)
assert run(comp4.maybe_compress(FakeContext(sid4))) is False
assert SessionStore.load_compact(CHANNEL, sid4) is None
print("maybe_compress disabled ok")

# 小窗口（trigger 为负）+ 历史能整体装进尾部预算 → 不压缩
fake5 = FakeProvider()
comp5 = make_compactor(fake5, window=2000)
sid5 = fresh_session("src-fits")
SessionStore.append(make_msg("user", "你好", session_id=sid5))
SessionStore.append(make_msg("assistant", "你好！", session_id=sid5))
assert run(comp5.maybe_compress(FakeContext(sid5))) is False
assert SessionStore.load_compact(CHANNEL, sid5) is None
print("maybe_compress fits-in-tail ok")

# 压缩后预估（system+压缩上限+尾部）仍超阈值 → 压缩无收益，放弃且不调 LLM
# window=6000, max_tokens=1000 → trigger=min(4500, 904)=904；tail=1500。
# 历史约 4400 超 trigger，但 post=1+500+2200=2701 ≥ 904。
fake6 = FakeProvider()
comp6 = make_compactor(fake6, window=6000, max_tokens=1000)
sid6 = fresh_session("src-nowin")
SessionStore.append(make_msg("user", "你好" * 1000, session_id=sid6))
SessionStore.append(make_msg("assistant", "答", session_id=sid6))
SessionStore.append(make_msg("user", "继续" * 1000, session_id=sid6))
assert run(comp6.maybe_compress(FakeContext(sid6))) is False
assert SessionStore.load_compact(CHANNEL, sid6) is None
assert fake6.payloads == []
print("maybe_compress no-gain ok")

# 压缩模型流正常结束但无 content（空压缩结果）→ 不落盘，历史不受损
fake7 = FakeProvider(chunks=[])
comp7 = make_compactor(fake7)
sid7 = fresh_session("src-empty")
seed_big(sid7)
assert run(comp7.maybe_compress(FakeContext(sid7))) is False
assert SessionStore.load_compact(CHANNEL, sid7) is None
assert len(SessionStore.load_history(sid7, CHANNEL)) == 5
print("maybe_compress empty-compact ok")

# 压缩记录落盘抛 OSError（磁盘满等）→ fail-safe，不影响主流程
orig_save = SessionStore.save_compact
SessionStore.save_compact = staticmethod(lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
fake8 = FakeProvider()
comp8 = make_compactor(fake8)
sid8 = fresh_session("src-diskfull")
seed_big(sid8)
assert run(comp8.maybe_compress(FakeContext(sid8))) is False
SessionStore.save_compact = orig_save
assert SessionStore.load_compact(CHANNEL, sid8) is None
print("maybe_compress save-failure ok")

# 输出被 max_tokens 截断（reasoning 吃光预算，usage 报满）→ 放弃，不落盘
fake_t = FakeProvider(chunks=[
    ("content", "# 摘要\n\n## Goal\n1. 用户（Ace）要求："),
    ("usage", TokenUsage(completion_tokens=4596, total_tokens=4596)),
])
comp_t = make_compactor(fake_t)  # context_compact_max=500 → budget=500+4096=4596
sid_t = fresh_session("src-truncated")
seed_big(sid_t)
assert run(comp_t.maybe_compress(FakeContext(sid_t))) is False
assert SessionStore.load_compact(CHANNEL, sid_t) is None
print("maybe_compress truncated ok")

# 无 usage 信号但内容是残句（缺 Progress 小节）→ 小节校验拒绝，旧压缩记录不被覆盖
fake_r1 = FakeProvider()
comp_r1 = make_compactor(fake_r1)
sid_r = fresh_session("src-partial")
seed_big(sid_r)
assert run(comp_r1.maybe_compress(FakeContext(sid_r))) is True
old_rec = SessionStore.load_compact(CHANNEL, sid_r)
SessionStore.append(make_msg("user", "追问" * 4000, session_id=sid_r))
SessionStore.append(make_msg("assistant", "第二轮", session_id=sid_r))
fake_r2 = FakeProvider(chunks=[("content", "# 摘要\n\n## Goal\n1. 用户（Ace）要求：")])
comp_r2 = make_compactor(fake_r2)
assert run(comp_r2.maybe_compress(FakeContext(sid_r))) is False
assert SessionStore.load_compact(CHANNEL, sid_r).covered_upto == old_rec.covered_upto
print("maybe_compress partial-keeps-old ok")

# 二次压缩：旧压缩记录作为 <previous_compact> 注入新压缩输入，covered_upto 前移
fake9 = FakeProvider()
comp9 = make_compactor(fake9)
sid9 = fresh_session("src-chain")
seed_big(sid9)
assert run(comp9.maybe_compress(FakeContext(sid9))) is True
first_covered = SessionStore.load_compact(CHANNEL, sid9).covered_upto
SessionStore.append(make_msg("user", "追问" * 4000, session_id=sid9))
SessionStore.append(make_msg("assistant", "第二轮", session_id=sid9))
assert run(comp9.maybe_compress(FakeContext(sid9))) is True
second_msgs = fake9.payloads[1].messages
assert any(m["role"] == "user" and "<previous_compact>" in m["content"] for m in second_msgs)
# 增量压缩：首次已覆盖的旧消息不再重送，仅送 covered_upto 之后的新消息
second_texts = [m.get("content") or "" for m in second_msgs]
assert not any(t == "你好" * 3500 for t in second_texts)
assert any(t == "继续" * 3500 for t in second_texts)
rec9 = SessionStore.load_compact(CHANNEL, sid9)
assert rec9.covered_upto != first_covered
print("maybe_compress chained ok")


# --- migrate_legacy_summaries ---------------------------------------------------
from zero.session.models import SessionSummary  # noqa: E402


def _write_legacy_summary(sid: str, content: str, covered: str) -> Path:
    """直接在会话目录写入旧版 SummaryRecord 结构的 summary.json。"""
    path = store_mod.SESSIONS_ROOT / CHANNEL
    for date_dir in path.iterdir():
        target = date_dir / sid / "summary.json"
        if target.parent.is_dir():
            target.write_text(
                json.dumps({"content": content, "covered_upto": covered,
                            "created_at": "2026-01-01T00:00:00"}, ensure_ascii=False),
                encoding="utf-8")
            return target
    raise AssertionError(f"session dir not found for {sid}")


# 旧结构 + 精确标签 → 改名 + 标签确定性改写为 <compact>
sid_l = fresh_session("src-legacy")
SessionStore.append(make_msg("user", "旧会话", session_id=sid_l))
m_l = make_msg("assistant", "回复", session_id=sid_l)
SessionStore.append(m_l)
legacy_path = _write_legacy_summary(
    sid_l, "<summary>\n## Goal\n测试\n</summary>", m_l.id)
assert SessionStore.migrate_legacy_summaries() >= 1
assert not legacy_path.exists()
migrated = SessionStore.load_compact(CHANNEL, sid_l)
assert migrated is not None
assert migrated.content == "<compact>\n## Goal\n测试\n</compact>"
assert migrated.covered_upto == m_l.id
# 迁移后 load_history 正常合成
hist = SessionStore.load_history(sid_l, CHANNEL, include_compact=True)
assert hist[0].metadata.get("kind") == "compact"
# 二次迁移幂等：已无旧文件可迁
assert SessionStore.migrate_legacy_summaries() >= 0
assert SessionStore.load_compact(CHANNEL, sid_l) is not None

# 新版 SessionSummary 的 summary.json → 不被误迁移
sid_n = fresh_session("src-newstyle")
SessionStore.append(make_msg("user", "新复盘", session_id=sid_n))
SessionStore.save_summary(CHANNEL, sid_n, SessionSummary(
    title="t", description="d", done=["a"], covered_upto="x"))
before = SessionStore._summary_file(CHANNEL, sid_n).read_text(encoding="utf-8")
SessionStore.migrate_legacy_summaries()
after = SessionStore._summary_file(CHANNEL, sid_n).read_text(encoding="utf-8")
assert before == after
assert not SessionStore._compact_file(CHANNEL, sid_n).exists()

# 旧结构但 content 标签不精确 → 只改名不改写
sid_p = fresh_session("src-partialfmt")
SessionStore.append(make_msg("user", "x", session_id=sid_p))
_write_legacy_summary(sid_p, "raw text without tags", "whatever")
SessionStore.migrate_legacy_summaries()
rec_p = SessionStore.load_compact(CHANNEL, sid_p)
assert rec_p is not None and rec_p.content == "raw text without tags"

# 损坏 JSON → 只改名，load_compact 安全返回 None
sid_b = fresh_session("src-broken")
SessionStore.append(make_msg("user", "y", session_id=sid_b))
broken = _write_legacy_summary(sid_b, "irrelevant", "x")
broken.write_text("{bad json", encoding="utf-8")
SessionStore.migrate_legacy_summaries()
assert not broken.exists()
assert (broken.parent / "compact.json").exists()
assert SessionStore.load_compact(CHANNEL, sid_b) is None
print("migrate_legacy_summaries ok")

print("ALL COMPACTOR TESTS PASSED")
