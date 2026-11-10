"""Offline tests for core/hooks.py P1 (no real LLM, isolated storage).

覆盖设计文档 docs/hooks.md 第 7 节的 P1 验收项：
  1. 回归：无插件（默认 Hooks / 空 HookChain）行为与现状一致
  2. 否决短路：首个 handler 否决后后续不再调用，tool.run 不执行，
     MessageTool 内容为 {"error": reason}；result 替换同理短路
  3. 仅附注（note）：不拦截，附注并入 tool 消息进入上下文
  4. 异常隔离：handler 抛异常不影响 loop 正常完成
  5. 截断不变量：hook 替换的超长结果仍被截到 10000 字符
"""
import asyncio
import json
from uuid import uuid4

import pytest

from zero.bus.queue import MessageBus
from zero.config import Config
from zero.core.context import Context
from zero.core.hooks import HOOK_NAMES, HookChain, Hooks, ToolCallDecision
from zero.core.runner import AgentRunner
from zero.models import ToolContext
from zero.tools.base import Tool
from zero.tools.registry import ToolRegistry
from tests.support import make_paths, make_permission, make_store

CHANNEL = "test"
TRUNC_SUFFIX = "...truncated"

# 一轮典型的两轮脚本：先一个 echo 工具调用，再输出最终文本
TOOL_CALL_CHUNK = {
    "index": 0,
    "id": "call_1",
    "type": "function",
    "function": {"name": "echo", "arguments": '{"x": 1}'},
}
ECHO_SCRIPT = [
    [("tool_calls", TOOL_CALL_CHUNK)],
    [("content", "最终回复")],
]


class StubProvider:
    """按脚本逐 iteration 吐出 (kind, data) 的假 provider。"""

    def __init__(self, script):
        self.script = [list(chunks) for chunks in script]

    async def stream_chunks(self, payload, **_kw):
        chunks = self.script.pop(0)
        for kind, data in chunks:
            yield kind, data


class EchoTool(Tool):
    def __init__(self):
        self.ran = False

    @property
    def name(self):
        return "echo"

    @property
    def description(self):
        return "echo back kwargs"

    @property
    def parameters(self):
        return {"type": "object", "properties": {"x": {"type": "integer"}}}

    async def run(self, context: ToolContext | None = None, **kwargs):
        self.ran = True
        return {"echo": kwargs}


def stub_config() -> Config:
    """最小可解析的根配置桩（models/agentRuntimes/providers 三件套齐备）。"""
    return Config.model_validate({
        "models": {"stub": {"provider": "deepseek", "contextWindowTokens": 100000}},
        "agentRuntimes": {"default": {"llmCallOptions": {"model": "stub"}}},
        "providers": {"deepseek": {"apiBase": "https://x.local", "apiKey": "k"}},
    })


def make_runner(hooks: Hooks, tool: EchoTool, session_id: str) -> AgentRunner:
    registry = ToolRegistry()
    registry.register(tool)
    context = Context(session_id=session_id, channel=CHANNEL,
                      tool_registry=registry, session_store=make_store(), paths=make_paths(), permission=make_permission())
    runner = AgentRunner(
        config=stub_config(),
        running_context=context,
        tool_registry=registry,
        hooks=hooks,
    )
    runner.provider = StubProvider(ECHO_SCRIPT)
    return runner


async def run_turn(runner: AgentRunner):
    return [event async for event in runner.run_stream()]


def history_summary(session_id: str):
    return [
        (m.sender, m.body.content,
         [tc.function.name for tc in (m.body.tool_calls or [])])
        for m in make_store().load_history(session_id, CHANNEL)
    ]


def new_session() -> str:
    return str(uuid4())


EXPECTED_EVENTS = ["tool_call", "delta", "done"]
EXPECTED_HISTORY = [
    ("assistant", "", ["echo"]),
    ("tool", json.dumps({"echo": {"x": 1}}, ensure_ascii=False), []),
]


# ---------------------------------------------------------------------------
# 1. 回归：默认 Hooks 与空 HookChain 行为一致
# ---------------------------------------------------------------------------

def test_regression_noop():
    for hooks in (Hooks(), HookChain()):
        tool = EchoTool()
        sid = new_session()
        events = asyncio.run(run_turn(make_runner(hooks, tool, sid)))
        assert [e.kind for e in events] == EXPECTED_EVENTS
        assert events[-1].content == "最终回复"
        assert tool.ran
        assert history_summary(sid) == EXPECTED_HISTORY


# ---------------------------------------------------------------------------
# 2. 否决短路 + Hooks 子类注册；result 替换短路
# ---------------------------------------------------------------------------

def test_veto_short_circuit():
    class SpyHooks(Hooks):
        """Hooks 子类写法：若被调用会记录下来。"""
        def __init__(self):
            self.calls = []

        async def before_tool_call(self, tool_name, kwargs, ctx):
            self.calls.append(tool_name)
            return None

    chain = HookChain()

    async def deny(tool_name, kwargs, ctx):
        assert tool_name == "echo" and kwargs == {"x": 1}
        assert ctx.channel == CHANNEL and ctx.session_id
        return ToolCallDecision(proceed=False, reason="blocked: test")

    spy = SpyHooks()
    chain.on_before_tool_call(deny)
    chain.register(spy)  # 注册顺序在 deny 之后，短路时不应被调用

    tool = EchoTool()
    sid = new_session()
    events = asyncio.run(run_turn(make_runner(chain, tool, sid)))

    assert spy.calls == [], "首个否决后后续 handler 不应被调用"
    assert not tool.ran, "否决时 tool.run 不应执行"
    assert [e.kind for e in events] == EXPECTED_EVENTS
    history = history_summary(sid)
    assert history[1] == (
        "tool", json.dumps({"error": "blocked: test"}, ensure_ascii=False), [])


def test_result_replacement():
    chain = HookChain()

    async def mock(tool_name, kwargs, ctx):
        return ToolCallDecision(result={"mock": True})

    chain.on_before_tool_call(mock)
    tool = EchoTool()
    sid = new_session()
    events = asyncio.run(run_turn(make_runner(chain, tool, sid)))

    assert not tool.ran, "result 替换时 tool.run 不应执行"
    assert [e.kind for e in events] == EXPECTED_EVENTS
    history = history_summary(sid)
    assert history[1] == (
        "tool", json.dumps({"mock": True}, ensure_ascii=False), [])


def test_decision_note_into_context():
    """仅附注（proceed=True, result=None）不拦截，附注并入 tool 消息进上下文。"""
    chain = HookChain()

    async def annotate_first(tool_name, kwargs, ctx):
        return ToolCallDecision(note="用户已批准扩权：/tmp/x")

    async def annotate_second(tool_name, kwargs, ctx):
        return ToolCallDecision(note="第二次审批已通过")

    chain.on_before_tool_call(annotate_first)
    chain.on_before_tool_call(annotate_second)
    tool = EchoTool()
    sid = new_session()
    asyncio.run(run_turn(make_runner(chain, tool, sid)))

    assert tool.ran, "仅附注不应阻止工具执行"
    payload = json.loads(history_summary(sid)[1][1])
    assert payload["echo"] == {"x": 1}, payload
    assert payload["permission"] == "用户已批准扩权：/tmp/x\n第二次审批已通过", payload


# ---------------------------------------------------------------------------
# 4. 异常隔离：handler 抛异常不影响 loop
# ---------------------------------------------------------------------------

def test_exception_isolation():
    chain = HookChain()

    async def boom(tool_name, kwargs, ctx):
        raise RuntimeError("plugin bug")

    chain.on_before_tool_call(boom)
    tool = EchoTool()
    sid = new_session()
    events = asyncio.run(run_turn(make_runner(chain, tool, sid)))

    assert [e.kind for e in events] == EXPECTED_EVENTS
    assert events[-1].content == "最终回复"
    assert tool.ran, "handler 异常应视为 no-op，工具照常执行"
    assert history_summary(sid) == EXPECTED_HISTORY


# ---------------------------------------------------------------------------
# 5. 截断不变量：hook 替换结果也逃不过 10000 字符截断
# ---------------------------------------------------------------------------

def test_truncation_after_hook():
    chain = HookChain()

    async def huge(tool_name, kwargs, ctx):
        return ToolCallDecision(result={"big": "x" * 20000})

    chain.on_before_tool_call(huge)
    tool = EchoTool()
    sid = new_session()
    asyncio.run(run_turn(make_runner(chain, tool, sid)))

    content = history_summary(sid)[1][1]
    assert content.endswith(TRUNC_SUFFIX)
    assert len(content) == 10000 + len(TRUNC_SUFFIX)


# ---------------------------------------------------------------------------
# 6. 注册期契约：HOOK_NAMES 与基类一致、async/参数个数、handlers_for
# ---------------------------------------------------------------------------

def test_hook_names_match_base_methods():
    public = {name for name in dir(Hooks) if not name.startswith("_")}
    assert set(HOOK_NAMES) == public


def test_sync_handler_rejected_at_registration():
    chain = HookChain()

    def sync_handler(tool_name, kwargs, ctx):
        return None

    with pytest.raises(TypeError, match="async"):
        chain.on_before_tool_call(sync_handler)


def test_wrong_arity_handler_rejected_at_registration():
    chain = HookChain()

    async def too_few(tool_name, kwargs):
        return None

    with pytest.raises(TypeError, match="3 个位置参数"):
        chain.on("before_tool_call", too_few)


def test_handlers_for_returns_registration_order():
    chain = HookChain()

    async def first(tool_name, kwargs, ctx):
        return None

    async def second(tool_name, kwargs, ctx):
        return None

    chain.on_before_tool_call(first)
    chain.on_before_tool_call(second)
    assert chain.handlers_for("before_tool_call") == (first, second)


def test_callable_object_with_async_call_is_accepted():
    """守卫是 async __call__ 的实例，注册校验不能只看实例本身。"""
    chain = HookChain()

    class Guard:
        async def __call__(self, tool_name, kwargs, ctx):
            return ToolCallDecision(proceed=False, reason="no")

    guard = Guard()
    chain.on_before_tool_call(guard)
    assert chain.handlers_for("before_tool_call") == (guard,)


def test_build_hooks_registers_real_guard():
    """真实组合根必须装上守卫（此前测试打桩了 install，漏掉了启动路径）。"""
    from zero.main import build_hooks

    chain = build_hooks(MessageBus(), make_permission())
    assert len(chain.handlers_for("before_tool_call")) == 1


def test_build_hooks_requires_guard(monkeypatch):
    """守卫装不上时启动期硬失败，而不是带着空链静默 fail-open。"""
    import zero.permission.guard as guard_mod
    from zero.main import build_hooks

    monkeypatch.setattr(guard_mod, "install", lambda chain, bus, *, permission: None)
    with pytest.raises(AssertionError, match="permission guard"):
        build_hooks(MessageBus(), make_permission())

