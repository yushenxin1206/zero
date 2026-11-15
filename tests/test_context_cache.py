"""Offline tests for the turn-level history snapshot in Context.

Context 每 turn 新建：首次访问 raw_history / compact_record / messages 时读
一次盘，之后 turn 内只走 record() 增量追加。本文件验证快照语义、压缩视图
重建、读盘次数与子 agent 隔离。
"""
import asyncio
import contextlib

from zero.core.compactor import Compactor
from zero.core.context import Context, SubAgentContext
from zero.data_paths import DataPaths
from tests.support import current, make_data_paths, make_store
from zero.models import (
    AgentRuntimeConfig,
    CompactConfig,
    CompactRecord,
    LLMCallOptions,
    LLMRuntime,
    Message,
    MessageBody,
    ProviderConfig,
    ToolCall,
    ToolCallFunction,
)
from zero.session.store import SessionStore, apply_compact

CHANNEL = "cli"


def fresh_session(source_id: str) -> str:
    return make_store().create_session(CHANNEL, source_id)


def make_msg(sender, content="", session_id="", tool_calls=None,
             tool_call_id=None):
    return Message(
        session_id=session_id,
        channel=CHANNEL,
        sender=sender,
        recipient="assistant" if sender != "assistant" else "user",
        body=MessageBody(
            content=content, tool_calls=tool_calls, tool_call_id=tool_call_id),
    )


def tc(name="read", args="{}") -> ToolCall:
    return ToolCall(id="call_1", function=ToolCallFunction(name=name, arguments=args))


@contextlib.contextmanager
def count_reads():
    """统计 make_store().load_history / load_compact 的调用次数。"""
    counter = {"history": 0, "compact": 0}
    orig_history = SessionStore.load_history
    orig_compact = SessionStore.load_compact

    def counted_history(self_, session_id, channel, include_compact=True,
                        repair_closed_turns=False, active_turn_id=""):
        counter["history"] += 1
        return orig_history(self_, session_id, channel, include_compact,
                            repair_closed_turns, active_turn_id)

    def counted_compact(self_, channel, session_id):
        counter["compact"] += 1
        return orig_compact(self_, channel, session_id)

    SessionStore.load_history = counted_history
    SessionStore.load_compact = counted_compact
    try:
        yield counter
    finally:
        SessionStore.load_history = orig_history
        SessionStore.load_compact = orig_compact


class FakeProvider:
    def __init__(self, chunks=None):
        self.chunks = chunks if chunks is not None else [
            ("content", "Goal: test\nProgress: nothing done")]

    async def stream_chunks(self, payload, **_kw):
        for kind, data in self.chunks:
            yield kind, data


def make_compactor(fake, window=20000, max_tokens=100,
                   context_compact_max=500, headroom=4096):
    rc = AgentRuntimeConfig(
        llm_call_options=LLMCallOptions(model="mock-model", max_tokens=max_tokens),
        llm_runtime=LLMRuntime(provider=ProviderConfig(),
                               context_window_tokens=window),
        reasoning_token_headroom=headroom,
    )
    cc = AgentRuntimeConfig(
        reasoning_token_headroom=headroom,
        llm_runtime=LLMRuntime(provider=ProviderConfig(),
                               context_window_tokens=window),
        llm_call_options=LLMCallOptions(model="mock-model"))
    cs = CompactConfig(context_compact_max_tokens=context_compact_max)
    return Compactor(provider=fake, session_runtime=rc, compact_runtime=cc,
                     compact_config=cs, session_store=make_store())


def seed_big(sid):
    """两轮历史：总估算 ~15400 tokens（窗口 20000，阈值 15000）。"""
    make_store().append(make_msg("user", "你好" * 3500, session_id=sid))
    make_store().append(make_msg("assistant", "第一轮回复", session_id=sid))
    make_store().append(make_msg("user", "继续" * 3500, session_id=sid))
    make_store().append(make_msg("assistant", "", session_id=sid,
                                 tool_calls=[tc()]))
    make_store().append(make_msg("tool", "结果" * 10, session_id=sid,
                                 tool_call_id="call_1"))


def test_apply_compact_pure_function():
    raw = [make_msg("user", "a"), make_msg("assistant", "b")]
    copied = apply_compact(raw, None, "sid", CHANNEL)
    assert copied == raw and copied is not raw          # 无压缩记录 → 全量副本

    rec = CompactRecord(content="<compact>x</compact>", covered_upto=raw[0].id)
    out = apply_compact(raw, rec, "sid", CHANNEL)
    assert len(out) == 2
    assert out[0].sender == "user" and out[0].compact
    assert out[0].body.content == "<compact>x</compact>"
    assert out[1].id == raw[1].id

    bad = CompactRecord(content="x", covered_upto="nope")
    assert len(apply_compact(raw, bad, "sid", CHANNEL)) == 2  # 定位失败 → 全量


def test_snapshot_semantics():
    """turn 内不感知外部写入，record() 立即反映。"""
    sid = fresh_session("src-snapshot")
    make_store().append(make_msg("user", "第一条", session_id=sid))
    ctx = Context(session_id=sid, channel=CHANNEL, session_store=make_store(), data_paths=make_data_paths(), working_dir=None, sandbox_available=True)

    assert [m["role"] for m in ctx.messages] == ["system", "user"]

    # 绕过 record 直接写盘（模拟另一进程 mid-turn 写入）→ 快照不变
    make_store().append(make_msg("user", "外部写入", session_id=sid))
    assert len(ctx.messages) == 2, "turn 内应保持一次原子快照"

    # 走 record → 立即反映（写透）
    ctx.record(make_msg("assistant", "回复", session_id=sid))
    assert len(ctx.messages) == 3
    assert ctx.messages[-1] == {"role": "assistant", "content": "回复"}
    # record 也写入磁盘（新 Context 能看到：system + 3 条消息）
    assert len(Context(session_id=sid, channel=CHANNEL, session_store=make_store(), data_paths=make_data_paths(), working_dir=None, sandbox_available=True).messages) == 4


def test_compaction_view_rebuild():
    """压缩回写后无需重读盘即可合成 [压缩] + 尾部。"""
    sid = fresh_session("src-compact-view")
    seed_big(sid)
    ctx = Context(session_id=sid, channel=CHANNEL, session_store=make_store(), data_paths=make_data_paths(), working_dir=None, sandbox_available=True)
    assert asyncio.run(
        make_compactor(FakeProvider()).maybe_compress(ctx)) is True

    with count_reads() as counter:
        msgs = ctx.messages
    assert counter["history"] == 0 and counter["compact"] == 0, \
        "回写后 messages 不应再读盘"
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user" and "<compact>" in msgs[1]["content"]
    assert all(m["role"] != "tool" for m in msgs[2:3])  # 尾部首条非 tool


def test_per_turn_single_read():
    """模拟完整 turn（N 次迭代）只读一次盘。"""
    sid = fresh_session("src-turn-count")
    make_store().append(make_msg("user", "任务", session_id=sid))
    ctx = Context(session_id=sid, channel=CHANNEL, session_store=make_store(), data_paths=make_data_paths(), working_dir=None, sandbox_available=True)
    comp = make_compactor(FakeProvider())

    with count_reads() as counter:
        for i in range(3):
            asyncio.run(comp.maybe_compress(ctx))    # 每次迭代的压缩检查
            assert ctx.messages[0]["role"] == "system"  # 每次迭代构建 payload
            ctx.record(make_msg("assistant", f"步骤{i}", session_id=sid))
    assert counter["history"] == 1, f"turn 内应只读一次历史，实际 {counter['history']}"
    assert counter["compact"] == 1, f"turn 内应只读一次压缩记录，实际 {counter['compact']}"
    assert len(ctx.messages) == 5                 # system + user + 3 assistant


def test_subagent_isolation():
    """子 agent 纯内存上下文不触盘。"""
    sub = SubAgentContext("子任务", session_store=make_store(), data_paths=make_data_paths(), sandbox_available=True)
    with count_reads() as counter:
        _ = sub.messages
        sub.record(make_msg("assistant", "子回复"))
        _ = sub.messages
    assert counter["history"] == 0 and counter["compact"] == 0, \
        "子 agent 不应读写会话存储"
    assert [m["role"] for m in sub.messages] == ["system", "user", "assistant"]


def test_environment_note_follows_settled_capability():
    """沙箱环境段只看组合根结算的事实，构建 prompt 不触发探针。"""
    def prompt(sandbox_available: bool) -> str:
        return Context(
            session_id="s-note", channel=CHANNEL,
            session_store=make_store(), data_paths=make_data_paths(),
            working_dir=None,
            sandbox_available=sandbox_available).system_prompt

    assert "Commands run inside the kernel sandbox" in prompt(True)
    assert "The kernel sandbox is unavailable here" in prompt(False)


def test_no_working_dir_omits_project_memory():
    """未选择工作目录：提示词明说未设置，且不渲染项目记忆段。"""
    ctx = Context(
        session_id="s-no-cwd", channel=CHANNEL,
        session_store=make_store(), data_paths=make_data_paths(),
        working_dir=None, sandbox_available=True)
    prompt = ctx.system_prompt
    assert "Working directory: (not set" in prompt
    assert "## Project Memory" not in prompt


def test_project_memory_follows_working_dir(tmp_path):
    """项目记忆路径跟随会话工作目录，而不是进程启动目录。"""
    memory = tmp_path / ".zero" / "memory" / "memory.md"
    memory.parent.mkdir(parents=True)
    memory.write_text("PROJECT-FACT", encoding="utf-8")
    data_paths = DataPaths(current().root)  # 移除测试的 project_memory 覆盖
    ctx = Context(
        session_id="s-cwd", channel=CHANNEL,
        session_store=make_store(), data_paths=data_paths,
        working_dir=tmp_path, sandbox_available=True)
    prompt = ctx.system_prompt
    assert f"Working directory: {tmp_path}" in prompt
    assert str(memory) in prompt
    assert "PROJECT-FACT" in prompt
