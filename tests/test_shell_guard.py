"""Offline tests for plugins/shell_guard (P2).

覆盖：
  - policy 三态判定：内置规则、用户规则优先级、deny > allow、glob 匹配
  - store 规则沉淀：add / remove / 去重 / 损坏文件兜底
  - handler：非 shell 放行、allow/deny、无人值守渠道 fail-closed、
    scheduler 即时拒绝、CLI 审批链路（y/n/a/w/超时）
  - 组合根集成：install() 接入 HookChain 后 runner 全链路行为

Run: python tests/test_shell_guard.py
"""
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import zero.core.context as ctx_mod
import zero.session.store as session_mod

# 隔离会话存储与记忆文件
_tmp = tempfile.TemporaryDirectory()
_tmp_root = Path(_tmp.name)
session_mod.SESSIONS_ROOT = _tmp_root / "sessions"
ctx_mod.ZERO_ROOT = _tmp_root

from zero.bus.queue import MessageBus
from zero.config import Config
from zero.core.context import Context
from zero.core.hooks import HookChain, HookContext
from zero.core.runner import AgentRunner
from zero.models import (
    ApprovalRequest, Origin, ToolContext)
from zero.plugins.shell_guard import approval, install, manage, policy, store
from zero.plugins.shell_guard.handler import ShellGuard
from zero.session.store import SessionStore
from zero.tools.base import Tool
from zero.tools.registry import ToolRegistry

CHANNEL = "cli"

# 用户规则重定向到临时文件，绝不碰真实的 ~/.zero/shell_guard.json
store.STORE_PATH = _tmp_root / "shell_guard.json"


def fresh_ctx(channel: str = CHANNEL, *, turn_id: str = "",
              origin: Origin | None = None) -> HookContext:
    session_id = str(uuid4())
    tc = ToolContext(
        channel=channel, session_id=session_id, turn_id=turn_id,
        origin=origin or Origin())
    return HookContext(
        channel=channel, session_id=session_id, turn_id=turn_id,
        tool_context=tc)


# ---------------------------------------------------------------------------
# policy：三态判定纯函数
# ---------------------------------------------------------------------------

def test_policy():
    # 内置 allow
    assert policy.evaluate("ls -la").tier == "allow"
    assert policy.evaluate("git status").tier == "allow"
    assert policy.evaluate("git  status").tier == "allow"  # 空白折叠
    # 内置 deny（glob）
    assert policy.evaluate("rm -rf /").tier == "deny"
    assert policy.evaluate("rm -rf /home/user").tier == "deny"
    assert policy.evaluate("shutdown -h now").tier == "deny"
    # ask
    v = policy.evaluate("npm install")
    assert v.tier == "ask" and v.reason
    # 空命令
    assert policy.evaluate("   ").tier == "deny"
    # 用户规则优先于内置规则
    assert policy.evaluate("git status", deny_rules=["git status*"]).tier == "deny"
    assert policy.evaluate("npm test", allow_rules=["npm test"]).tier == "allow"
    # 含 glob 字符的命令走精确相等，不被误当模式
    assert policy.evaluate("echo a*b", allow_rules=["echo a*b"]).tier == "allow"
    print("policy ok")


# ---------------------------------------------------------------------------
# store：规则沉淀
# ---------------------------------------------------------------------------

def test_store():
    assert store.rules() == ([], [])  # 文件不存在 → 空
    assert store.add("allow", "npm test")
    assert not store.add("allow", "npm test")  # 去重
    store.add("deny", "docker *")
    allow, deny = store.rules()
    assert allow == ["npm test"] and deny == ["docker *"]
    assert store.remove("allow", 0) == "npm test"
    assert store.remove("allow", 9) is None  # 越界
    assert store.rules() == ([], ["docker *"])
    # 损坏文件兜底为空
    store.STORE_PATH.write_text("{broken", encoding="utf-8")
    assert store.rules() == ([], [])
    store.STORE_PATH.unlink()
    print("store ok")


# ---------------------------------------------------------------------------
# handler：三态与渠道分发
# ---------------------------------------------------------------------------

def test_handler_passthrough_and_builtin():
    guard = ShellGuard(MessageBus())

    async def run():
        # 非 shell 工具直接放行
        assert await guard("read", {}, fresh_ctx()) is None
        # 内置 allow
        assert await guard("shell", {"command": "ls"}, fresh_ctx()) is None
        # 内置 deny
        d = await guard("shell", {"command": "rm -rf /"}, fresh_ctx())
        assert d is not None and not d.proceed and "blocked" in d.reason
        # 无人值守渠道 fail-closed
        d = await guard("shell", {"command": "npm install"},
                        fresh_ctx(channel="feishu"))
        assert d is not None and not d.proceed and "fail-closed" in d.reason
        # scheduler 来源即时拒绝（不进审批、不发事件）
        d = await guard("shell", {"command": "npm install"},
                        fresh_ctx(origin=Origin(kind="scheduler", task_id="t-9")))
        assert d is not None and not d.proceed and "定时任务" in d.reason

    asyncio.run(run())
    print("handler tiers ok")


# ---------------------------------------------------------------------------
# handler：CLI 审批链路（y / n / a / w / 超时）
# ---------------------------------------------------------------------------

async def _ask_roundtrip(command: str, answer: str | None):
    """跑一次 ask 审批；answer=None 表示不作答（超时路径）。

    注意每轮必须用不同命令：审批答 a/w 会把命令沉淀进规则，
    同一命令再问会直接命中规则、不再发审批事件。
    """
    bus = MessageBus()
    guard = ShellGuard(bus)
    ctx = fresh_ctx(turn_id="t-1")
    task = asyncio.create_task(
        guard("shell", {"command": command}, ctx))

    if answer is None:
        approval.TIMEOUT = 0.05
        try:
            event = await bus.stream_queue.dequeue()
            return event, await task
        finally:
            approval.TIMEOUT = 180

    event = await bus.stream_queue.dequeue()
    req = ApprovalRequest.model_validate_json(event.content)
    assert bus.approvals.resolve(req.req_id, answer)
    return event, await task


def test_approval_flow():
    async def run():
        # y：放行一次，不沉淀规则
        event, d = await _ask_roundtrip("npm install", "y")
        assert event.kind == "approval" and event.turn_id == "t-1"
        req = ApprovalRequest.model_validate_json(event.content)
        assert req.summary == "npm install"
        assert [o.key for o in req.options] == ["y", "a", "s", "t", "n", "d"]
        # deadline 是 CLI 倒计时的权威时点，由插件端统一签发
        assert req.deadline > time.time()
        assert d is None
        assert store.rules() == ([], [])

        # n：拒绝一次
        _, d = await _ask_roundtrip("npm test", "n")
        assert d is not None and not d.proceed and "denied by user" in d.reason

        # a：放行并沉淀 allow 规则
        _, d = await _ask_roundtrip("npm run build", "a")
        assert d is None
        assert store.rules()[0] == ["npm run build"]
        # 沉淀后同命令直接放行，不再发问
        guard = ShellGuard(MessageBus())
        assert await guard(
            "shell", {"command": "npm run build"}, fresh_ctx()) is None

        # d：拒绝并沉淀 deny 规则
        _, d = await _ask_roundtrip("npm publish", "d")
        assert d is not None and not d.proceed and "always" in d.reason
        assert store.rules() == (["npm run build"], ["npm publish"])

        # 超时：fail-closed
        _, d = await _ask_roundtrip("make all", None)
        assert d is not None and not d.proceed and "超时" in d.reason

    asyncio.run(run())
    store.STORE_PATH.unlink(missing_ok=True)
    print("approval flow ok")


# ---------------------------------------------------------------------------
# 组合根集成：install() 接入 HookChain，runner 全链路
# ---------------------------------------------------------------------------

class StubShell(Tool):
    """名为 "shell" 的假工具，不真的起子进程。"""

    def __init__(self):
        self.ran = False

    @property
    def name(self):
        return "shell"

    @property
    def description(self):
        return "stub shell"

    @property
    def parameters(self):
        return {"type": "object", "properties": {}}

    async def run(self, context: ToolContext | None = None, **kwargs):
        self.ran = True
        return {"ok": True, "command": kwargs.get("command", "")}


class StubProvider:
    def __init__(self, command: str):
        self.script = [
            [("tool_calls", {
                "index": 0, "id": "c1", "type": "function",
                "function": {"name": "shell",
                             "arguments": json.dumps({"command": command})},
            })],
            [("content", "完毕")],
        ]

    async def stream_chunks(self, payload, **_kw):
        for kind, data in self.script.pop(0):
            yield kind, data


def stub_config() -> Config:
    """最小可解析的根配置桩（models/agentRuntimes/providers 三件套齐备）。"""
    return Config.model_validate({
        "models": {"stub": {"provider": "deepseek", "contextWindowTokens": 100000}},
        "agentRuntimes": {"default": {"llmCallOptions": {"model": "stub"}}},
        "providers": {"deepseek": {"apiBase": "https://x.local", "apiKey": "k"}},
    })


def _run_turn_with_guard(command: str):
    chain = HookChain()
    install(chain, MessageBus())
    tool = StubShell()
    registry = ToolRegistry()
    registry.register(tool)
    sid = str(uuid4())
    context = Context(session_id=sid, channel=CHANNEL, tool_registry=registry)
    runner = AgentRunner(
        config=stub_config(),
        running_context=context,
        tool_registry=registry,
        hooks=chain,
    )
    runner.provider = StubProvider(command)

    async def go():
        return [e async for e in runner.run_stream()]

    events = asyncio.run(go())
    history = SessionStore.load_history(sid, CHANNEL)
    tool_msg = next(m for m in history if m.sender == "tool")
    return tool, events, tool_msg.body.content


def test_runner_integration():
    # 内置 deny：命令被拦截，工具未执行，错误回执进入会话历史
    tool, events, content = _run_turn_with_guard("rm -rf /")
    assert not tool.ran
    assert "shell_guard: blocked" in content
    assert events[-1].kind == "done" and events[-1].content == "完毕"

    # 内置 allow：工具正常执行
    tool, events, content = _run_turn_with_guard("ls")
    assert tool.ran
    assert json.loads(content)["ok"] is True
    print("runner integration ok")


# ---------------------------------------------------------------------------
# zero-guard 管理命令
# ---------------------------------------------------------------------------

def test_manage():
    manage.main(["add", "allow", "npm test"])
    manage.main(["add", "deny", "docker *"])
    assert store.rules() == (["npm test"], ["docker *"])
    manage.main(["remove", "allow", "0"])
    assert store.rules() == ([], ["docker *"])
    manage.main(["list"])  # 不抛异常即可
    store.STORE_PATH.unlink(missing_ok=True)
    print("manage ok")


if __name__ == "__main__":
    test_policy()
    test_store()
    test_handler_passthrough_and_builtin()
    test_approval_flow()
    test_runner_integration()
    test_manage()
    print("all shell_guard tests passed")
