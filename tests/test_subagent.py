"""Offline tests for subagent single-shot invocation (no real LLM, isolated storage).

覆盖 run_subagent / SubAgentContext 的核心验收项：
  1. 单次调用：prompt 唯一输入，工具循环正常运转，返回最终文本
  2. 隔离：中间消息只留内存，不读写 SessionStore，父会话历史不受影响
  3. 自定义 system_prompt 生效
  4. 失败抛 RuntimeError，不返回残缺回复
  5. 压缩跳过：子 agent 不触发 maybe_compress（会话模式照常触发，回归）
  6. tool_context 继承：工具收到的 context 带父轮路由字段
  7. hooks 照常生效：否决短路、错误结果留在内存对话里

Run: python tests/test_subagent.py
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import zero.core.context as ctx_mod
import zero.core.runner as runner_mod
import zero.session.store as store_mod

# 隔离存储与记忆文件：全部重定向到临时目录
_tmp = tempfile.TemporaryDirectory()
_tmp_root = Path(_tmp.name)
store_mod.SESSIONS_ROOT = _tmp_root / "sessions"
ctx_mod.MEMORY_PATH = _tmp_root / "memory.md"

from zero.core.compactor import Compactor
from zero.core.context import Context, SubAgentContext
from zero.core.hooks import HookChain, ToolCallDecision
from zero.core.runner import AgentRunner, run_subagent
from zero.models import (
    AgentRuntimeConfig,
    LLMConfig,
    MessageBody,
    MessageIn,
    ProviderConfigs,
    ToolContext,
)
from zero.session.store import SessionStore
from zero.tools.base import Tool
from zero.tools.registry import ToolRegistry

CHANNEL = "test"

TOOL_CALL_CHUNK = {
    "index": 0,
    "id": "call_1",
    "type": "function",
    "function": {"name": "echo", "arguments": '{"x": 1}'},
}


class ScriptedProvider:
    """按脚本逐 iteration 吐出 (kind, data)，并记录每次收到的 payload。"""

    def __init__(self, script):
        self.script = [list(chunks) for chunks in script]
        self.payloads = []

    async def stream_chunks(self, payload):
        self.payloads.append(payload)
        chunks = self.script.pop(0)
        for kind, data in chunks:
            yield kind, data


class EchoTool(Tool):
    def __init__(self):
        self.ran = False
        self.contexts = []

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
        self.contexts.append(context)
        return {"echo": kwargs}


def make_registry(tool) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(tool)
    return registry


def stub_config() -> AgentRuntimeConfig:
    return AgentRuntimeConfig(
        provider="dashscope", llm_config=LLMConfig(model="stub"))


def install_provider(*scripts) -> list[ScriptedProvider]:
    """把 runner 模块的 OpenAI 换成脚本化桩；每次构造消费一份脚本。

    返回按构造顺序累积的桩实例列表（scripts 每项是一个 iteration 的
    chunk 列表的列表）。
    """
    queue = [list(s) for s in scripts]
    made: list[ScriptedProvider] = []

    def factory(_cfg):
        p = ScriptedProvider(queue.pop(0) if queue else [[("content", "")]])
        made.append(p)
        return p

    runner_mod.OpenAI = factory
    return made


def install_compactor_spy() -> dict:
    """把 runner 模块的 Compactor 换成计数桩，返回调用记录 dict。"""
    holder = {"calls": 0}

    class SpyCompactor(Compactor):
        async def maybe_compress(self, context):
            holder["calls"] += 1
            return False

    runner_mod.Compactor = SpyCompactor
    return holder


def snapshot_files() -> list[str]:
    """sessions 目录下所有相对路径快照（隔离性断言用）。"""
    root = _tmp_root / "sessions"
    if not root.exists():
        return []
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


# ---------------------------------------------------------------------------
# 1. 单次调用：工具循环正常运转，第二次迭代收到完整内存对话
# ---------------------------------------------------------------------------

def test_single_shot_with_tools():
    tool = EchoTool()
    providers = install_provider([
        [("tool_calls", TOOL_CALL_CHUNK)],
        [("content", "子代理完成")],
    ])
    reply = asyncio.run(run_subagent(
        "做点事",
        runtime_config=stub_config(),
        provider_configs=ProviderConfigs(),
        tool_registry=make_registry(tool),
    ))
    assert reply == "子代理完成"
    assert tool.ran

    provider = providers[0]
    assert len(provider.payloads) == 2
    roles = [m["role"] for m in provider.payloads[1].messages]
    assert roles == ["system", "user", "assistant", "tool"]
    assert provider.payloads[1].messages[1]["content"] == "做点事"
    assert provider.payloads[1].messages[3]["tool_call_id"] == "call_1"
    print("1. single-shot with tool loop ok")


# ---------------------------------------------------------------------------
# 2. 隔离：不读写 SessionStore，父会话历史不受影响
# ---------------------------------------------------------------------------

def test_isolation_from_session_store():
    sid = SessionStore.create_session(CHANNEL, "parent-src")
    SessionStore.append(MessageIn(
        session_id=sid, channel=CHANNEL,
        body=MessageBody(content="父轮消息")))

    tool = EchoTool()
    install_provider([
        [("tool_calls", TOOL_CALL_CHUNK)],
        [("content", "done")],
    ])
    before = snapshot_files()
    reply = asyncio.run(run_subagent(
        "子任务",
        runtime_config=stub_config(),
        provider_configs=ProviderConfigs(),
        tool_registry=make_registry(tool),
        tool_context=ToolContext(channel=CHANNEL, session_id=sid, turn_id="t1"),
    ))
    after = snapshot_files()

    assert reply == "done"
    assert before == after, "子 agent 不得在 sessions 目录产生任何新文件"
    history = SessionStore.load_history(sid, CHANNEL)
    assert len(history) == 1 and history[0].body.content == "父轮消息"
    print("2. isolation from SessionStore ok")


# ---------------------------------------------------------------------------
# 3. 自定义 system_prompt
# ---------------------------------------------------------------------------

def test_custom_system_prompt():
    providers = install_provider([[("content", "ok")]])
    asyncio.run(run_subagent(
        "翻译任务",
        runtime_config=stub_config(),
        provider_configs=ProviderConfigs(),
        system_prompt="你是翻译代理，只输出译文。",
    ))
    first = providers[0].payloads[0].messages[0]
    assert first == {"role": "system", "content": "你是翻译代理，只输出译文。"}
    print("3. custom system_prompt ok")


# ---------------------------------------------------------------------------
# 4. 失败抛 RuntimeError
# ---------------------------------------------------------------------------

def test_error_raises():
    class BoomProvider:
        async def stream_chunks(self, payload):
            raise RuntimeError("provider down")
            yield  # noqa: unreachable — 使其成为 async generator

    runner_mod.OpenAI = lambda _cfg: BoomProvider()
    try:
        asyncio.run(run_subagent(
            "必败任务",
            runtime_config=stub_config(),
            provider_configs=ProviderConfigs(),
        ))
        raise AssertionError("run_subagent 应当抛 RuntimeError")
    except RuntimeError as e:
        assert "provider down" in str(e)
    print("4. error raises RuntimeError ok")


# ---------------------------------------------------------------------------
# 5. 压缩跳过：子 agent 不触发 maybe_compress；会话模式照常触发（回归）
# ---------------------------------------------------------------------------

def test_compression_gate():
    spy = install_compactor_spy()
    install_provider([[("content", "ok")]])
    asyncio.run(run_subagent(
        "任务",
        runtime_config=stub_config(),
        provider_configs=ProviderConfigs(),
    ))
    assert spy["calls"] == 0, "子 agent 不应触发上下文压缩"

    # 回归：会话模式（AgentLoop 同款构造）照常触发
    registry = make_registry(EchoTool())
    context = Context(
        session_id=str(uuid4()), channel=CHANNEL, tool_registry=registry)
    runner = AgentRunner(
        runtime_config=stub_config(),
        provider_configs=ProviderConfigs(),
        running_context=context,
        tool_registry=registry,
    )

    async def drain():
        return [e async for e in runner.run_stream()]

    events = asyncio.run(drain())
    assert events[-1].kind == "done"
    assert spy["calls"] == 1, "会话模式每个 iteration 应调用压缩检查"
    print("5. compression gate ok")


# ---------------------------------------------------------------------------
# 6. tool_context 继承：工具/hook 收到父轮路由字段
# ---------------------------------------------------------------------------

def test_tool_context_inherited():
    tool = EchoTool()
    install_provider([
        [("tool_calls", TOOL_CALL_CHUNK)],
        [("content", "done")],
    ])
    seen = {}

    chain = HookChain()

    async def observe(tool_name, kwargs, ctx):
        seen["turn_id"] = ctx.turn_id
        seen["channel"] = ctx.channel
        return None

    chain.on_before_tool_call(observe)
    parent_tc = ToolContext(
        channel=CHANNEL, session_id="sid-x", turn_id="turn-42")
    asyncio.run(run_subagent(
        "任务",
        runtime_config=stub_config(),
        provider_configs=ProviderConfigs(),
        tool_registry=make_registry(tool),
        hooks=chain,
        tool_context=parent_tc,
    ))
    assert tool.contexts[0].turn_id == "turn-42"
    assert tool.contexts[0].channel == CHANNEL
    assert seen["turn_id"] == "turn-42" and seen["channel"] == CHANNEL
    print("6. tool_context inherited ok")


# ---------------------------------------------------------------------------
# 7. hooks 照常生效：否决短路，错误结果留在内存对话
# ---------------------------------------------------------------------------

def test_hooks_apply_to_subagent():
    chain = HookChain()

    async def deny(tool_name, kwargs, ctx):
        return ToolCallDecision(proceed=False, reason="blocked: subagent")

    chain.on_before_tool_call(deny)
    tool = EchoTool()
    providers = install_provider([
        [("tool_calls", TOOL_CALL_CHUNK)],
        [("content", "被否决后的回复")],
    ])
    reply = asyncio.run(run_subagent(
        "任务",
        runtime_config=stub_config(),
        provider_configs=ProviderConfigs(),
        tool_registry=make_registry(tool),
        hooks=chain,
    ))
    assert not tool.ran
    assert reply == "被否决后的回复"
    tool_msg = providers[0].payloads[1].messages[3]
    assert json.loads(tool_msg["content"]) == {"error": "blocked: subagent"}
    print("7. hooks apply to subagent ok")


if __name__ == "__main__":
    test_single_shot_with_tools()
    test_isolation_from_session_store()
    test_custom_system_prompt()
    test_error_raises()
    test_compression_gate()
    test_tool_context_inherited()
    test_hooks_apply_to_subagent()
    print("all subagent tests passed")
