"""Offline tests for subagent single-shot invocation (no real LLM,
isolated storage via conftest).

覆盖 run_subagent / SubAgentContext 的核心验收项：
  1. 单次调用：prompt 唯一输入，工具循环正常运转，返回最终文本
  2. 隔离：中间消息只留内存，不读写 SessionStore，父会话历史不受影响
  3. 自定义 system_prompt 生效
  4. 失败抛 RuntimeError，不返回残缺回复
  5. 压缩跳过：子 agent 不触发 maybe_compress（会话模式照常触发，回归）
  6. tool_context 继承：工具收到的 context 带父轮路由字段
  7. hooks 照常生效：否决短路、错误结果留在内存对话里
"""
import asyncio
import json
from pathlib import Path
from uuid import uuid4

import zero.core.runner as runner_mod
from zero.bus.queue import MessageBus
from zero.config import Config
from zero.core.compactor import Compactor
from zero.core.context import Context
from zero.core.hooks import HookChain, HookContext, ToolCallDecision
from zero.core.runner import AgentRunner, run_subagent
from zero.models import MessageBody, MessageIn, ToolContext
from zero.paths import current
from zero.session.store import SessionStore
from zero.subagent.manager import SubagentManager
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

    async def stream_chunks(self, payload, **_kw):
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


def stub_config() -> Config:
    """最小可解析的根配置桩（models/agentRuntimes/providers 三件套齐备）。"""
    return Config.model_validate({
        "models": {"stub": {"provider": "deepseek", "contextWindowTokens": 100000}},
        "agentRuntimes": {"default": {"llmCallOptions": {"model": "stub"}}},
        "providers": {"deepseek": {"apiBase": "https://x.local", "apiKey": "k"}},
    })


def install_provider(monkeypatch, *scripts) -> list[ScriptedProvider]:
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

    monkeypatch.setattr(runner_mod, "OpenAI", factory)
    return made


def install_compactor_spy(monkeypatch) -> dict:
    """把 runner 模块的 Compactor 换成计数桩，返回调用记录 dict。"""
    holder = {"calls": 0}

    class SpyCompactor(Compactor):
        async def maybe_compress(self, context):
            holder["calls"] += 1
            return False

    monkeypatch.setattr(runner_mod, "Compactor", SpyCompactor)
    return holder


def snapshot_files() -> list[str]:
    """sessions 目录下所有相对路径快照（隔离性断言用）。"""
    root = current().sessions
    if not root.exists():
        return []
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


# ---------------------------------------------------------------------------
# 1. 单次调用：工具循环正常运转，第二次迭代收到完整内存对话
# ---------------------------------------------------------------------------

def test_single_shot_with_tools(monkeypatch):
    tool = EchoTool()
    providers = install_provider(monkeypatch, [
        [("tool_calls", TOOL_CALL_CHUNK)],
        [("content", "子代理完成")],
    ])
    reply = asyncio.run(run_subagent(
        "做点事",
        config=stub_config(),
        # 根配置：运行时配置在 AgentRunner 内按 task 解析
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


# ---------------------------------------------------------------------------
# 2. 隔离：不读写 SessionStore，父会话历史不受影响
# ---------------------------------------------------------------------------

def test_isolation_from_session_store(monkeypatch):
    sid = SessionStore.create_session(CHANNEL, "parent-src")
    SessionStore.append(MessageIn(
        session_id=sid, channel=CHANNEL,
        body=MessageBody(content="父轮消息")))

    tool = EchoTool()
    install_provider(monkeypatch, [
        [("tool_calls", TOOL_CALL_CHUNK)],
        [("content", "done")],
    ])
    before = snapshot_files()
    reply = asyncio.run(run_subagent(
        "子任务",
        config=stub_config(),
        # 根配置：运行时配置在 AgentRunner 内按 task 解析
        tool_registry=make_registry(tool),
        tool_context=ToolContext(channel=CHANNEL, session_id=sid, turn_id="t1"),
    ))
    after = snapshot_files()

    assert reply == "done"
    assert before == after, "子 agent 不得在 sessions 目录产生任何新文件"
    history = SessionStore.load_history(sid, CHANNEL)
    assert len(history) == 1 and history[0].body.content == "父轮消息"


# ---------------------------------------------------------------------------
# 3. 自定义 system_prompt
# ---------------------------------------------------------------------------

def test_custom_system_prompt(monkeypatch):
    providers = install_provider(monkeypatch, [[("content", "ok")]])
    asyncio.run(run_subagent(
        "翻译任务",
        config=stub_config(),
        # 根配置：运行时配置在 AgentRunner 内按 task 解析
        system_prompt="你是翻译代理，只输出译文。",
    ))
    first = providers[0].payloads[0].messages[0]
    assert first == {"role": "system", "content": "你是翻译代理，只输出译文。"}


# ---------------------------------------------------------------------------
# 4. 失败抛 RuntimeError
# ---------------------------------------------------------------------------

def test_error_raises(monkeypatch):
    class BoomProvider:
        async def stream_chunks(self, payload, **_kw):
            raise RuntimeError("provider down")
            yield  # 不可达：仅用于声明 async generator

    monkeypatch.setattr(runner_mod, "OpenAI", lambda _cfg: BoomProvider())
    try:
        asyncio.run(run_subagent(
            "必败任务",
            config=stub_config(),
            # 根配置：运行时配置在 AgentRunner 内按 task 解析
        ))
        raise AssertionError("run_subagent 应当抛 RuntimeError")
    except RuntimeError as e:
        assert "provider down" in str(e)


# ---------------------------------------------------------------------------
# 5. 压缩跳过：子 agent 不触发 maybe_compress；会话模式照常触发（回归）
# ---------------------------------------------------------------------------

def test_compression_gate(monkeypatch):
    spy = install_compactor_spy(monkeypatch)
    # 每次构造 AgentRunner 会消耗两份脚本：self.provider + compactor.provider
    # （见 AgentRunner.__init__）。故子 agent 与随后的会话 runner 各需两份。
    install_provider(
        monkeypatch,
        [[("content", "ok")]],           # 子 agent self.provider
        [[("content", "")]],             # 子 agent compactor.provider（未使用）
        [[("content", "会话答复")]],      # 会话 self.provider
        [[("content", "")]],             # 会话 compactor.provider（未使用）
    )
    asyncio.run(run_subagent(
        "任务",
        config=stub_config(),
        # 根配置：运行时配置在 AgentRunner 内按 task 解析
    ))
    assert spy["calls"] == 0, "子 agent 不应触发上下文压缩"

    # 回归：会话模式（AgentLoop 同款构造）照常触发
    registry = make_registry(EchoTool())
    context = Context(
        session_id=str(uuid4()), channel=CHANNEL, tool_registry=registry)
    runner = AgentRunner(
        config=stub_config(),
        # 根配置：运行时配置在 AgentRunner 内按 task 解析
        running_context=context,
        tool_registry=registry,
    )

    async def drain():
        return [e async for e in runner.run_stream()]

    events = asyncio.run(drain())
    assert events[-1].kind == "done"
    assert spy["calls"] == 1, "会话模式每个 iteration 应调用压缩检查"


# ---------------------------------------------------------------------------
# 6. tool_context 继承：工具/hook 收到父轮路由字段
# ---------------------------------------------------------------------------

def test_tool_context_inherited(monkeypatch):
    tool = EchoTool()
    install_provider(monkeypatch, [
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
        config=stub_config(),
        # 根配置：运行时配置在 AgentRunner 内按 task 解析
        tool_registry=make_registry(tool),
        hooks=chain,
        tool_context=parent_tc,
    ))
    assert tool.contexts[0].turn_id == "turn-42"
    assert tool.contexts[0].channel == CHANNEL
    # run_subagent 原样透传父轮 tool_context（含 origin）。把 origin 盖章为
    # subagent（以便 permission.guard 对需审批命令 fail-closed）是派发方
    # SubagentManager 的职责，见测试 10；执行层不改写调用方给的路由字段。
    assert tool.contexts[0].origin.kind == "user"
    assert seen["turn_id"] == "turn-42" and seen["channel"] == CHANNEL


# ---------------------------------------------------------------------------
# 7. hooks 照常生效：否决短路，错误结果留在内存对话
# ---------------------------------------------------------------------------

def test_hooks_apply_to_subagent(monkeypatch):
    chain = HookChain()

    async def deny(tool_name, kwargs, ctx):
        return ToolCallDecision(proceed=False, reason="blocked: subagent")

    chain.on_before_tool_call(deny)
    tool = EchoTool()
    providers = install_provider(monkeypatch, [
        [("tool_calls", TOOL_CALL_CHUNK)],
        [("content", "被否决后的回复")],
    ])
    reply = asyncio.run(run_subagent(
        "任务",
        config=stub_config(),
        # 根配置：运行时配置在 AgentRunner 内按 task 解析
        tool_registry=make_registry(tool),
        hooks=chain,
    ))
    assert not tool.ran
    assert reply == "被否决后的回复"
    tool_msg = providers[0].payloads[1].messages[3]
    assert json.loads(tool_msg["content"]) == {"error": "blocked: subagent"}


# ---------------------------------------------------------------------------
# 8. 空正文/截断不再静默返回：run_subagent 应抛 RuntimeError
# ---------------------------------------------------------------------------

def test_empty_reply_raises(monkeypatch):
    # 仅 reasoning、无正文（reasoning 典型吃满预算后被截空的形态）：
    # runner 现在产出 error 事件，run_subagent 据此抛 RuntimeError，
    # 而不是把一个空字符串当成有效结果回投。
    install_provider(
        monkeypatch,
        [[("reasoning", "只有思考")]],   # 子 agent self.provider：空正文
        [[("content", "")]],             # compactor.provider（未使用）
    )
    try:
        asyncio.run(run_subagent("写一篇作文", config=stub_config()))
        raise AssertionError("空正文应抛 RuntimeError")
    except RuntimeError as e:
        assert "空正文" in str(e) or "截断" in str(e), \
            f"错误信息应说明空正文/截断，实际：{e}"


# ---------------------------------------------------------------------------
# 9. subagent 来源的 shell 需审批命令 → fail-closed（不发审批、不挂起）
# ---------------------------------------------------------------------------

def test_subagent_shell_fail_closed():
    from zero.permission.guard import PermissionGuard
    from zero.models import Origin

    guard = PermissionGuard(msg_bus=object())   # 有总线，但无人值守来源应短路
    ctx = HookContext(
        channel="cli", session_id="sid", turn_id="sub-1",
        tool_context=ToolContext(
            channel="cli", session_id="sid", turn_id="sub-1",
            origin=Origin(kind="subagent", task_id="sub-1")),
        stop_event=None,
    )
    # 需要扩权（G 档）的命令在无人值守来源下须立即拒绝，不得等待审批
    target = str(Path.home() / "zero-subagent-selftest")
    decision = asyncio.run(guard("shell", {
        "command": "echo x", "write_paths": [target]}, ctx))
    assert decision is not None and not decision.proceed, \
        "subagent 的需审批命令应 fail-closed"
    assert "无人值守" in decision.reason, f"拒绝原因应说明无人值守：{decision.reason}"
    # 非 shell 工具不受影响
    assert asyncio.run(guard("read", {"path": "x"}, ctx)) is None


# ---------------------------------------------------------------------------
# 10. manager 派发：工具上下文 origin 盖章 subagent，回投/结果事件同源
# ---------------------------------------------------------------------------

def test_manager_stamps_subagent_origin(monkeypatch):
    tool = EchoTool()
    bus = MessageBus()
    install_provider(monkeypatch, [
        [("tool_calls", TOOL_CALL_CHUNK)],
        [("content", "子任务完成")],
    ])
    manager = SubagentManager(
        msg_bus=bus,
        config=stub_config(),
        tool_registry=make_registry(tool),
        hooks=HookChain(),
    )
    # 父轮是 cli 且带总线：若 origin 未被盖章为 subagent，permission.guard 会走
    # 就地审批并在后台挂起——正是本次要修的现象
    parent_tc = ToolContext(
        channel="cli", session_id="sid-cli", turn_id="turn-9")

    async def drive() -> str:
        launched = manager.launch(
            [{"prompt": "做点事", "description": "d"}], tool_context=parent_tc)
        sid = launched[0]["id"]
        await manager._tasks[sid]        # 等待子任务跑完并完成回投
        return sid

    sid = asyncio.run(drive())
    # 工具在子 agent 内执行时收到的 origin 已被盖章为 subagent（含任务 id）
    assert tool.contexts[0].origin.kind == "subagent"
    assert tool.contexts[0].origin.task_id == sid
    # 回投消息与结果事件都带 subagent origin，channel 端据此区分后台来源
    msg = bus.in_queue.queue.get_nowait()
    assert msg.origin.kind == "subagent" and msg.origin.task_id == sid
    event = bus.stream_queue.queue.get_nowait()
    assert event.kind == "subagent_result" and event.origin.kind == "subagent"

