"""Offline tests for core/hooks.py P1 (no real LLM, isolated storage).

覆盖设计文档 docs/hooks.md 第 7 节的 P1 验收项：
  1. 回归：无插件（默认 Hooks / 空 HookChain）行为与现状一致
  2. 否决短路：首个 handler 否决后后续不再调用，tool.run 不执行，
     MessageTool 内容为 {"error": reason}；result 替换同理短路
  4. 异常隔离：handler 抛异常不影响 loop 正常完成
  5. 截断不变量：hook 替换的超长结果仍被截到 10000 字符

Run: python tests/test_hooks.py
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import zero.core.context as ctx_mod
import zero.session.store as store_mod

# 隔离存储与记忆文件：全部重定向到临时目录
_tmp = tempfile.TemporaryDirectory()
_tmp_root = Path(_tmp.name)
store_mod.SESSIONS_ROOT = _tmp_root / "sessions"
ctx_mod.ZERO_ROOT = _tmp_root

from zero.core.context import Context
from zero.core.hooks import HookChain, Hooks, ToolCallDecision
from zero.core.runner import AgentRunner
from zero.models import AgentRuntimeConfig, LLMConfig, ProviderConfig, ToolContext
from zero.session.store import SessionStore
from zero.tools.base import Tool
from zero.tools.registry import ToolRegistry

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

    async def stream_chunks(self, payload):
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


def make_runner(hooks: Hooks, tool: EchoTool, session_id: str) -> AgentRunner:
    registry = ToolRegistry()
    registry.register(tool)
    context = Context(session_id=session_id, channel=CHANNEL, tool_registry=registry)
    runner = AgentRunner(
        runtime_config=AgentRuntimeConfig(
            provider=ProviderConfig(api_base="https://x.local", api_key="k"),
            llm_config=LLMConfig(model="stub")),
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
        for m in SessionStore.load_history(session_id, CHANNEL)
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
    print("1. regression (default Hooks / empty HookChain) ok")


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
    print("2a. veto short-circuit ok")


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
    print("2b. result replacement ok")


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
    print("4. exception isolation ok")


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
    print("5. truncation invariant ok")


if __name__ == "__main__":
    test_regression_noop()
    test_veto_short_circuit()
    test_result_replacement()
    test_exception_isolation()
    test_truncation_after_hook()
    print("all hooks tests passed")
