"""Offline tests for zero/permission/（envelope + seatbelt + guard）。

覆盖：
  1. 渲染：deny default / 写集合加法 / 不可授予项与 profile.json 在 deny 列表
  2. 判定：check_readable / check_writable 的 P / G / D
  3. 包裹：wrap 产出 sandbox-exec -f <profile>
  4. 探针：available() 与真实 profile 一致（macOS 上必须通过）
  5. guard：非 shell 放行 / 普通命令 P 放行 / 无人值守 fail-closed
  6. guard：D 档不进面板 / G 档「本会话允许」落表 / 拒绝不改状态
  7. shell.py：内核拒绝时回投 sandbox_denied + needed
  8. launchd 变更面：读允许 / 写拒绝（macOS 语义翻转的报警线）
  9. 文件类工具走审批门：read D 档 / write G 档授权 / 无人值守
 10. 覆盖漏接检测：带 *_path 参数的工具必须在 HANDLED_TOOLS 里
 11. 失败语义：守卫内部异常 → 拒绝（fail-closed）
"""
import ast
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from tests.support import current, make_scheduler_store, make_store

import zero.channels as channels_pkg
from zero.channels.service import ChannelService
from zero.permission import envelope, seatbelt
from zero.permission.envelope import PermissionState

from zero.bus.queue import MessageBus
from zero.core.hooks import HookContext
import zero.models as contract
from zero.models import (
    ORIGIN_SCHEDULER, ORIGIN_SUBAGENT, ORIGIN_USER, ApprovalRequest, Origin,
    OriginKind, ToolContext)
from zero.permission.guard import HANDLED_TOOLS, PermissionGuard
from zero.tools.shell import Shell
from zero.tools.write import Write

DARWIN_ONLY = pytest.mark.skipif(
    sys.platform != "darwin", reason="Seatbelt / launchd 仅 macOS")

REPO_ROOT = Path(__file__).resolve().parents[1]


def current_envelope(ctx, permission: PermissionState):
    """测试边界：用显式权限状态推导包络。"""
    return permission.envelope(ctx)


def check_writable(path, ctx, permission: PermissionState):
    return permission.check_writable(path, ctx)


def make_shell(permission: PermissionState) -> Shell:
    return Shell(permission=permission)

# 无人值守渠道：不能就地弹审批面板，也不放行网络。新增渠道必须显式归类
# （加入 ATTENDED_CHANNELS 或这里），否则下面的覆盖测试会失败。
UNATTENDED_CHANNELS = frozenset({"feishu"})


def _declared_channel_names() -> set[str]:
    """扫描 zero/channels 下的 CHANNEL 声明。

    取值支持字面量或共享常量（``CHANNEL = CHANNEL_CLI``）：常量统一在
    zero.models 里定义，这里按名字解析。
    """
    root = Path(channels_pkg.__file__).parent
    names: set[str] = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            targets = node.targets
            if not any(isinstance(t, ast.Name) and t.id == "CHANNEL"
                       for t in targets):
                continue
            value: object = None
            if isinstance(node.value, ast.Constant):
                value = node.value.value
            elif isinstance(node.value, ast.Name):
                value = getattr(contract, node.value.id, None)
            if isinstance(value, str):
                names.add(value)
    return names


def test_every_channel_is_capability_classified():
    declared = _declared_channel_names()
    assert declared, "未扫描到任何渠道的 CHANNEL 声明"
    assert declared == envelope.ATTENDED_CHANNELS | UNATTENDED_CHANNELS, (
        "渠道能力未归类：新增渠道必须加入 envelope.ATTENDED_CHANNELS，"
        "或本测试的 UNATTENDED_CHANNELS")


def test_unattended_channels_never_prompt_or_network(permission):
    for name in UNATTENDED_CHANNELS:
        ctx = ToolContext(channel=name)
        assert not envelope.attended(ctx), name
        assert not current_envelope(ctx, permission).network, name


def make_ctx(channel: str = "cli", kind: OriginKind = ORIGIN_USER) -> HookContext:
    session_id = str(uuid4())
    tc = ToolContext(channel=channel, session_id=session_id,
                     turn_id="t1", origin=Origin(kind=kind),
                     working_dir=str(REPO_ROOT))
    return HookContext(channel=channel, session_id=session_id, turn_id="t1",
                       tool_context=tc)


def answer_next(bus: MessageBus, key: str) -> asyncio.Task:
    """等下一个审批事件并以 key 作答（在事件循环里跑）。"""
    async def worker():
        event = await bus.stream_queue.dequeue()
        req = ApprovalRequest.model_validate_json(event.content)
        bus.approvals.resolve(req.req_id, key)
    return asyncio.create_task(worker())


def outside(name: str) -> str:
    """包络外路径（在 HOME 下、不在项目根内）。"""
    return str(Path.home() / name)


def test_permission_cli_accepts_data_paths(capsys, permission):
    """CLI 入口支持显式 DataPaths：不读默认 ~/.zero。"""
    from zero.permission.envelope import main

    assert main(["list"], data_paths=permission.data_paths) == 0
    out = capsys.readouterr().out
    assert str(envelope._real(permission.data_paths.profile_file)) in out


# ---------------------------------------------------------------------------
# 1. 渲染
# ---------------------------------------------------------------------------

def test_render(permission):
    env = envelope.Envelope(
        workspace=Path("/tmp/proj"),
        session_dir=Path("/tmp/proj/sess"),
        deny_read=tuple(envelope._real(p) for p in permission.ungrantable_read()))
    text = seatbelt.render_profile(env, permission.data_paths.profiles)
    # 渲染值一律 realpath 后的字面量（macOS：/tmp → /private/tmp）
    root = seatbelt._quote(envelope._real("/tmp/proj"))
    sess = seatbelt._quote(envelope._real("/tmp/proj/sess"))
    policy = seatbelt._quote(envelope._real(current().profile_file))
    ssh = seatbelt._quote(envelope._real(envelope.HOME / ".ssh"))
    assert "(deny default)" in text
    assert "(allow file-read*)" in text
    assert "(allow file-write*" in text
    assert "(allow network*)" in text
    assert f"(subpath {root})" in text
    assert f"(subpath {sess})" in text
    # 策略文件与凭证必须出现在 deny 列表（防自举越狱）
    assert f"(subpath {policy})" in text, "profile.json 未被 deny"
    assert f"(subpath {ssh})" in text
    # mach：不得裸放（会开到 securityd），凭证服务必须显式 deny
    assert "\n(allow mach-lookup)\n" not in text, "裸放 mach-lookup 会开到 securityd"
    assert '(global-name "com.apple.securityd")' in text
    assert '(global-name "com.apple.SecurityServer")' in text


def test_render_denied_network(permission):
    """无人值守档必须显式断网，且压过基线里可能的网络放行。"""
    env = envelope.Envelope(workspace=Path("/tmp/proj"), network=False)
    text = seatbelt.render_profile(env, permission.data_paths.profiles)
    assert "(allow network*)" not in text
    assert "(deny network*)" in text


def test_network_policy_by_source(permission):
    """网络放行与「能否弹审批」共用同一判定：只有交互渠道的用户轮次放行。"""
    assert envelope.attended(make_ctx())
    assert current_envelope(make_ctx(), permission).network
    assert current_envelope(ToolContext(channel="cli"), permission).network

    unattended = (
        ToolContext(channel="cli", origin=Origin(kind=ORIGIN_SCHEDULER)),
        ToolContext(channel="cli", origin=Origin(kind=ORIGIN_SUBAGENT)),
        ToolContext(channel="feishu"),
        None,
    )
    for ctx in unattended:
        assert not envelope.attended(ctx), ctx
        assert not current_envelope(ctx, permission).network, ctx


# ---------------------------------------------------------------------------
# 2. 判定 P / G / D
# ---------------------------------------------------------------------------

def test_scope(permission):
    v = permission.check_readable(envelope.HOME / ".ssh" / "id_rsa")
    assert not v.ok and v.tier == envelope.D, v
    assert v.needed

    v = permission.check_readable(REPO_ROOT / "zero/permission/envelope.py")
    assert v.ok and v.tier == envelope.P, v

    v = check_writable(
        str(REPO_ROOT / "zero/permission/envelope.py"), make_ctx(), permission)
    assert v.ok, v

    v = check_writable("/etc/hosts", None, permission)
    assert not v.ok and v.tier == envelope.D, v

    v = check_writable(outside("zero-out-selftest"), None, permission)
    assert not v.ok and v.tier == envelope.G, v


def test_validate_workspace(tmp_path, permission):
    """用户选择的工作目录必须是具体项目目录，不能被过宽的根撑破沙箱。"""
    good = tmp_path / "project"
    good.mkdir()
    assert (envelope.validate_workspace(str(good), permission.data_paths)
            == envelope._real(good))

    permission.data_paths.root.mkdir(parents=True, exist_ok=True)
    for bad in ("relative/path", str(tmp_path / "missing"),
                str(envelope.HOME), str(permission.data_paths.root)):
        with pytest.raises(ValueError):
            envelope.validate_workspace(bad, permission.data_paths)

    not_dir = tmp_path / "file.txt"
    not_dir.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        envelope.validate_workspace(str(not_dir), permission.data_paths)


def test_policy_edit_roundtrip(tmp_path, permission):
    """长期策略的增删必须落盘、可撤销，且重复撤销是幂等的。"""
    root = tmp_path / "shared"
    root.mkdir()
    permission.add_extra_root(root)
    assert envelope._real(root) in permission.extra_roots()
    assert permission.remove_extra_root(str(root)) is True
    assert permission.extra_roots() == []
    assert permission.remove_extra_root(str(root)) is False

    permission.add_unsandbox("brew")
    assert permission.unsandbox_names() == ["brew"]
    assert permission.remove_unsandbox(" brew ") is True
    assert permission.unsandbox_names() == []
    assert permission.remove_unsandbox("brew") is False


def test_permission_admin_validation(tmp_path, permission):
    """GUI 权限入口沿用工作目录的边界校验，坏输入必须当场拒绝。"""
    service = ChannelService(
        "web",
        msg_bus=MessageBus(),
        session_store=make_store(),
        scheduler_store=make_scheduler_store(),
        data_paths=permission.data_paths,
        permission=permission,
    )
    root = tmp_path / "shared"
    root.mkdir()
    assert service.grant_write_root(str(root)) == str(envelope._real(root))
    assert service.revoke_write_root(str(root)) is True

    with pytest.raises(ValueError):
        service.grant_write_root("relative/path")
    with pytest.raises(ValueError):
        service.grant_write_root(str(envelope.HOME))

    with pytest.raises(ValueError):
        service.grant_unsandbox("")
    with pytest.raises(ValueError):
        service.grant_unsandbox("/usr/bin/brew")
    with pytest.raises(ValueError):
        service.grant_unsandbox("brew --verbose")

    assert service.grant_unsandbox(" brew ") == "brew"
    snapshot = service.permission_snapshot()
    assert snapshot["unsandbox"] == ["brew"]
    assert snapshot["extraRoots"] == []
    assert "sandbox" in snapshot


def test_web_permission_protocol(tmp_path, permission):
    """Web 权限页协议：快照 + 增删 + 坏输入回显错误。"""
    from zero.channels.web.server import WebChannel

    class FakeWS:
        def __init__(self):
            self.sent = []

        async def send(self, text):
            self.sent.append(json.loads(text))

    async def main():
        channel = WebChannel(
            msg_bus=MessageBus(),
            session_store=make_store(),
            scheduler_store=make_scheduler_store(),
            data_paths=permission.data_paths,
            permission=permission,
        )
        ws = FakeWS()
        root = tmp_path / "shared"
        root.mkdir()

        await channel._dispatch(ws, {"type": "permission"})
        read = ws.sent[-1]
        assert read["type"] == "permission" and read["ok"] is True
        assert "sandbox" in read["snapshot"]

        await channel._dispatch(ws, {
            "type": "permission", "op": "add-root", "path": str(root)})
        added = ws.sent[-1]
        assert added["ok"] is True
        assert str(envelope._real(root)) in added["snapshot"]["extraRoots"]

        await channel._dispatch(ws, {
            "type": "permission", "op": "remove-root", "path": str(root)})
        assert ws.sent[-1]["snapshot"]["extraRoots"] == []

        await channel._dispatch(ws, {
            "type": "permission", "op": "add-unsandbox", "name": "brew"})
        assert ws.sent[-1]["snapshot"]["unsandbox"] == ["brew"]

        await channel._dispatch(ws, {
            "type": "permission", "op": "add-unsandbox",
            "name": "/usr/bin/brew"})
        bad = ws.sent[-1]
        assert bad["ok"] is False and "命令名" in bad["error"]

    asyncio.run(main())


def test_envelope_workspace_optional(permission):
    """未选目录时没有项目写区；选定后写集合才包含该目录。"""
    no_ws = ToolContext(channel="cli", session_id=str(uuid4()))
    env = permission.envelope(no_ws)
    assert env.workspace is None
    assert str(envelope._real(REPO_ROOT)) not in {str(r) for r in env.write_roots()}

    with_ws = ToolContext(
        channel="cli", session_id=str(uuid4()), working_dir=str(REPO_ROOT))
    assert permission.envelope(with_ws).workspace == envelope._real(REPO_ROOT)
    target = str(REPO_ROOT / "zero-stageB-inside.txt")
    assert permission.check_writable(target, with_ws).ok
    assert not permission.check_writable(target, no_ws).ok


def test_guard_rejects_relative_paths(permission):
    """文件工具与 shell 路径参数只收绝对路径。"""
    async def main():
        guard = PermissionGuard(None, permission=permission)
        cases = (
            ("read", {"file_path": "README.md"}),
            ("write", {"file_path": "out.txt", "content": "x"}),
            ("edit", {"file_path": "out.txt", "old_string": "a",
                      "new_string": "b"}),
            ("shell", {"command": "ls", "working_dir": "relative"}),
            ("shell", {"command": "ls", "write_paths": ["relative"]}),
        )
        for tool, kwargs in cases:
            decision = await guard(tool, kwargs, make_ctx())
            assert decision is not None and decision.proceed is False, (tool, decision)
            assert "绝对路径" in decision.reason, (tool, decision.reason)

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 3. 包裹
# ---------------------------------------------------------------------------

def test_wrap(permission):
    body = "(version 1)\n(allow default)\n"
    argv, path = seatbelt.wrap(
        ["/bin/echo", "hi"], body, permission.data_paths.profiles)
    assert argv[0] == str(seatbelt.SEATBELT_EXEC)
    assert argv[1] == "-f" and argv[2] == str(path)
    assert argv[3:] == ["/bin/echo", "hi"]
    assert path.read_text(encoding="utf-8") == body
    path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 4. 探针
# ---------------------------------------------------------------------------

def test_probe(permission):
    if sys.platform != "darwin":
        assert not permission.sandbox_available()
        return
    assert permission.sandbox_available(), permission.sandbox_unavailable_reason()


@DARWIN_ONLY
def test_network_probe(permission):
    """断网档双向探针：宿主能连、沙箱内必须连不上。"""
    if not permission.sandbox_available():
        pytest.skip("内核沙箱不可用，跳过断网探针")
    assert permission.network_enforced(), "断网档未生效（(deny network*) 没起作用）"


# ---------------------------------------------------------------------------
# 5 / 6. guard 档位
# ---------------------------------------------------------------------------

def test_guard_basics(permission):
    async def main():
        bus = MessageBus()
        guard = PermissionGuard(bus, permission=permission)

        assert await guard("read", {}, make_ctx()) is None, "非 shell 应放行"

        if permission.sandbox_available():
            assert await guard("shell", {"command": "ls -la"},
                               make_ctx()) is None, "P 档应放行"

        # 无人值守 + 需扩权 → fail-closed，且不进面板
        ctx = make_ctx(kind=ORIGIN_SCHEDULER)
        worker = answer_next(bus, "s")
        d = await guard("shell", {"command": "echo x",
                                  "write_paths": [outside("zero-out-selftest")]},
                        ctx)
        assert d is not None and d.proceed is False, d
        assert "无人值守" in d.reason, d.reason
        assert bus.stream_queue.size == 0, "无人值守不应进面板"
        assert not worker.done()
        worker.cancel()

    asyncio.run(main())


def test_guard_unattended_expand_fail_closed(permission):
    """非 CLI 渠道同样无人值守。"""
    async def main():
        bus = MessageBus()
        guard = PermissionGuard(bus, permission=permission)
        ctx = make_ctx(channel="feishu")
        d = await guard("shell", {"command": "echo x",
                                  "write_paths": [outside("zero-out-selftest")]},
                        ctx)
        assert d is not None and d.proceed is False, d

    asyncio.run(main())


def test_guard_tiers(permission):
    if not permission.sandbox_available():
        pytest.skip("内核沙箱不可用，跳过 D/G 档语义")

    async def main():
        bus = MessageBus()
        guard = PermissionGuard(bus, permission=permission)

        # D 档：不可授予 → 拒绝，且绝不进面板
        ctx = make_ctx()
        worker = answer_next(bus, "s")
        d = await guard("shell", {"command": "echo x",
                                  "write_paths": ["/etc/hosts"]}, ctx)
        assert d is not None and d.proceed is False, d
        assert "不可授予" in d.reason, d.reason
        assert bus.stream_queue.size == 0, "D 档不应进面板"
        assert not worker.done()
        worker.cancel()

        # G 档 + 「本会话允许」→ 放行并落会话扩权表
        ctx = make_ctx()
        target = outside("zero-out-selftest-a")
        worker = answer_next(bus, "s")
        d = await guard("shell", {"command": "echo x",
                                  "write_paths": [target]}, ctx)
        await worker
        assert d is not None and d.proceed, d
        assert "本会话" in d.note, d.note
        assert permission.session_roots(ctx), "「本会话允许」应写入会话扩权表"
        assert check_writable(target, ctx, permission).ok, "扩权后该路径应可写"
        permission.reset_session(ctx)

        # G 档 + 拒绝 → 拒绝，且不改状态
        ctx = make_ctx()
        target2 = outside("zero-out-selftest-b")
        worker = answer_next(bus, "n")
        d = await guard("shell", {"command": "echo x",
                                  "write_paths": [target2]}, ctx)
        await worker
        assert d is not None and d.proceed is False, d
        assert not check_writable(target2, ctx, permission).ok

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 7. shell.py 回投
# ---------------------------------------------------------------------------

@DARWIN_ONLY
def test_shell_denied_roundtrip(permission):
    if not permission.sandbox_available():
        pytest.skip("内核沙箱不可用，跳过 shell 拒绝回投")

    shell = make_shell(permission)
    ctx = ToolContext(
        channel="cli", session_id=str(uuid4()), working_dir=str(REPO_ROOT))

    # 包络外写：内核拒绝 → sandbox_denied + needed
    result = asyncio.run(shell.run(
        context=ctx, command=f"/usr/bin/touch {outside('zero-out-selftest')}"))
    assert result["returncode"] != 0, result
    assert result.get("sandbox_denied") is True, result
    assert "write_paths" in result["needed"], result

    # 包络内写：正常成功，且不得被标注为拒绝
    inside = Path("zero-permission-selftest.tmp")
    result = asyncio.run(shell.run(
        context=ctx, command=f"/usr/bin/touch {inside.resolve()}"))
    assert result["returncode"] == 0, result
    assert "sandbox_denied" not in result, result
    Path(inside).unlink(missing_ok=True)


@DARWIN_ONLY
def test_shell_network_by_source(permission):
    """端到端：同一联网命令，交互来源成功，无人值守来源被内核拒绝。

    用 ``python -c`` 而不是 nc：单程序即可"连接 + 读取"，正是评审文档里
    指出的外泄形态；且拒绝时 stderr 带 ``Operation not permitted``，能同时
    验证 sandbox_denied 与断网 hint 的回投链路。
    """
    if not permission.sandbox_available():
        pytest.skip("内核沙箱不可用，跳过断网端到端")

    import shlex
    import socket

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = listener.getsockname()[1]
    code = f"import socket; socket.create_connection(('127.0.0.1', {port}), 2)"
    command = shlex.join([sys.executable, "-B", "-c", code])
    try:
        attended_ctx = ToolContext(
            channel="cli", session_id=str(uuid4()), working_dir=str(REPO_ROOT),
            origin=Origin(kind=ORIGIN_USER))
        result = asyncio.run(
            make_shell(permission).run(context=attended_ctx, command=command))
        assert result["returncode"] == 0, result

        unattended_ctx = ToolContext(
            channel="cli", session_id=str(uuid4()), working_dir=str(REPO_ROOT),
            origin=Origin(kind=ORIGIN_SCHEDULER))
        result = asyncio.run(
            make_shell(permission).run(context=unattended_ctx, command=command))
        assert result["returncode"] != 0, result
        assert result.get("sandbox_denied") is True, result
        assert "断网" in result["needed"], result
    finally:
        listener.close()


# ---------------------------------------------------------------------------
# 8. launchd 变更面：读允许 / 写拒绝（报警线）
# ---------------------------------------------------------------------------

@DARWIN_ONLY
def test_launchd_mutation_blocked(isolated_state, permission):
    """锁住「launchd 只能读、不能变」这一语义。

    实测（本机 macOS）：沙箱内 `launchctl print gui/<uid>` 成功——读走的是每个
    进程都继承的 bootstrap port；而 `bootstrap` / `submit` 被拒——写需要
    privileged bootstrap right。这个区分**不是我们设计的**，macOS 升级一旦翻转
    它没有任何东西会报警，而翻转即意味着沙箱能被"让 launchd 替我跑沙箱外进程"
    完整绕过。本测试就是那道报警线。

    对照步（先跑沙箱外）是刻意的：证明该向量本身真实存在，避免"沙箱内失败"
    是因为命令压根不可用而变成空测试。
    """
    if not permission.sandbox_available():
        pytest.skip("内核沙箱不可用，跳过 launchd 变更面")

    uid = os.getuid()
    tag = uuid4().hex[:8]
    label = f"com.example.zero-permission-probe.{tag}"
    plist = isolated_state / f"zero-permission-probe-{tag}.plist"
    escape = Path.home() / f"zero-permission-escape-{tag}.txt"
    plist.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        f'  <key>Label</key><string>{label}</string>\n'
        '  <key>ProgramArguments</key>\n'
        '  <array><string>/bin/sh</string><string>-c</string>'
        f'<string>/usr/bin/touch {escape}</string></array>\n'
        '  <key>RunAtLoad</key><true/>\n'
        '</dict>\n</plist>\n', encoding="utf-8")

    text = seatbelt.render_profile(
        current_envelope(None, permission), permission.data_paths.profiles)

    def sandboxed(argv):
        wrapped, profile = seatbelt.wrap(
            argv, text, permission.data_paths.profiles)
        try:
            return subprocess.run(wrapped, capture_output=True, timeout=30)
        finally:
            profile.unlink(missing_ok=True)

    def bootout():
        subprocess.run(["/bin/launchctl", "bootout", f"gui/{uid}/{label}"],
                       capture_output=True, timeout=30)

    try:
        # 对照：沙箱外同一 plist 确实能让 launchd 写出包络外文件
        escape.unlink(missing_ok=True)
        bootout()
        out = subprocess.run(["/bin/launchctl", "bootstrap", f"gui/{uid}",
                              str(plist.resolve())],
                             capture_output=True, timeout=30)
        control_ok = out.returncode == 0
        time.sleep(1.5)
        control_wrote = escape.exists()
        bootout()
        escape.unlink(missing_ok=True)
        if not control_ok or not control_wrote:
            # 沙箱外都做不到 → 该环境（如无 GUI 会话的 CI）无法复现这条
            # 攻击向量，测试失去意义。跳过而非失败，避免把环境限制误报成
            # 安全回归；开发机上仍然强制执行。
            pytest.skip("对照步不可复现：当前环境无法 bootstrap launchd job")

        # 沙箱内：bootstrap 必须被拒，且不得写出任何东西
        r = sandboxed(["/bin/launchctl", "bootstrap", f"gui/{uid}",
                       str(plist.resolve())])
        assert r.returncode != 0, (
            "沙箱内 launchctl bootstrap 竟然成功了——launchd 可代跑沙箱外进程，"
            "权限边界已失效（macOS 行为可能已变更）")
        assert not escape.exists(), "逃逸文件出现：沙箱可被越过"

        # submit 同样必须被拒
        r = sandboxed(["/bin/launchctl", "submit", "-l", f"{label}.submit", "--",
                       "/bin/sh", "-c", f"/usr/bin/touch {escape}"])
        assert r.returncode != 0, "沙箱内 launchctl submit 竟然成功了"
        assert not escape.exists(), "逃逸文件出现：沙箱可被越过"
    finally:
        bootout()
        subprocess.run(["/bin/launchctl", "remove", f"{label}.submit"],
                       capture_output=True, timeout=30)
        escape.unlink(missing_ok=True)
        plist.unlink(missing_ok=True)



# ---------------------------------------------------------------------------
# 9. 阶段 B：进程内工具越界改走审批门（第 1 层）
# ---------------------------------------------------------------------------

def test_file_tools_use_approval_gate(permission):
    """read/write/edit 越界不再只是 fail-closed，而是走审批门。

    读方向是减法（全开减不可授予），所以只有 D 档、永不进面板；
    写方向才有 G 档，审批通过后落会话扩权表，同会话内后续同路径免审批。
    """
    async def main():
        bus = MessageBus()
        guard = PermissionGuard(bus, permission=permission)

        # read 到凭证目录：D 档拒绝，不进面板
        ctx = make_ctx()
        worker = answer_next(bus, "s")
        d = await guard("read",
                        {"file_path": str(envelope.HOME / ".ssh" / "id_rsa")}, ctx)
        assert d is not None and d.proceed is False, d
        assert "不可授予" in d.reason, d.reason
        assert bus.stream_queue.size == 0, "读方向 D 档不应进面板"
        assert not worker.done()
        worker.cancel()

        # read 包络内：放行，不弹面板
        worker = answer_next(bus, "s")
        assert await guard(
            "read", {"file_path": str(REPO_ROOT / "zero/permission/envelope.py")},
            make_ctx()) is None
        assert bus.stream_queue.size == 0, "包络内不应进面板"
        assert not worker.done()
        worker.cancel()

        # write 到系统路径：D 档拒绝，不进面板
        ctx = make_ctx()
        worker = answer_next(bus, "s")
        d = await guard("write", {"file_path": "/etc/zero-stage-b-test",
                                  "content": "x"}, ctx)
        assert d is not None and d.proceed is False, d
        assert "不可授予" in d.reason, d.reason
        assert bus.stream_queue.size == 0, "写方向 D 档不应进面板"
        assert not worker.done()
        worker.cancel()

        # write 越界 + 无人值守：fail-closed
        d = await guard("write", {"file_path": outside("zero-stageB-unattended.txt"),
                                  "content": "x"}, make_ctx(kind=ORIGIN_SCHEDULER))
        assert d is not None and d.proceed is False, d
        assert "无人值守" in d.reason, d.reason

        # write 包络内：放行，不弹面板
        worker = answer_next(bus, "s")
        assert await guard(
            "write", {"file_path": str(REPO_ROOT / "zero-stageB-inside.txt"),
                      "content": "x"}, make_ctx()) is None
        assert bus.stream_queue.size == 0, "包络内不应进面板"
        assert not worker.done()
        worker.cancel()

        # write 越界 + 「本会话允许」→ 放行 + 落表 + 工具端到端真能写出去
        target = Path(outside("zero-stageB-granted.txt"))
        target.unlink(missing_ok=True)
        ctx = make_ctx()
        worker = answer_next(bus, "s")
        d = await guard("write", {"file_path": str(target), "content": "ok"}, ctx)
        await worker
        assert d is not None and d.proceed, d
        assert str(target) in d.note, d.note
        assert check_writable(str(target), ctx, permission).ok, "授权后该路径应可写"
        result = await Write().run(context=ctx.tool_context,
                                   file_path=str(target), content="ok")
        assert result.get("success") is True, result
        assert target.read_text(encoding="utf-8") == "ok"
        target.unlink(missing_ok=True)

        # write 越界 + 拒绝 → 拒绝，且不改状态
        ctx = make_ctx()
        target2 = outside("zero-stageB-rejected.txt")
        worker = answer_next(bus, "n")
        d = await guard("write", {"file_path": target2, "content": "x"}, ctx)
        await worker
        assert d is not None and d.proceed is False, d
        assert not check_writable(target2, ctx, permission).ok

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 10 / 11. 覆盖与失败语义
# ---------------------------------------------------------------------------

def test_guard_covers_all_file_tools(permission):
    """漏接检测：注册表里任何带 *_path 参数的工具都必须在 HANDLED_TOOLS 里。

    守卫按工具名分发，所以"新增一个文件类工具却忘了登记"不会报错，只会**静默
    失去检查**。这条测试就是那道报警线。
    """
    from zero.main import build_tool_registry

    registry = build_tool_registry(
        make_store(), make_scheduler_store(), permission)
    covered = 0
    missing: list[str] = []
    for schema in registry.schemas():
        fn = schema.get("function", schema)
        name = fn.get("name", "")
        props = (fn.get("parameters") or {}).get("properties") or {}
        if name and any(k.endswith("_path") for k in props):
            covered += 1
            if name not in HANDLED_TOOLS:
                missing.append(name)
    assert not missing, (
        f"这些工具带 *_path 参数却没被守卫处理：{missing}——"
        f"请登记进 permission.guard.HANDLED_TOOLS，或说明为何无需检查")
    assert covered >= 3, f"只找到 {covered} 个带路径参数的工具，测试可能已失效"


def test_guard_fail_closed_on_internal_error(monkeypatch, permission):
    """守卫内部异常必须变成拒绝，不能被 hook 的 fail-open 吞成放行。"""
    def boom(*_a, **_kw):
        raise RuntimeError("injected guard bug")

    monkeypatch.setattr(PermissionState, "check_readable", boom)
    guard = PermissionGuard(MessageBus(), permission=permission)
    decision = asyncio.run(
        guard("read", {"file_path": str(REPO_ROOT / "zero/permission/envelope.py")},
              make_ctx()))

    assert decision is not None and decision.proceed is False, decision
    assert "fail-closed" in decision.reason, decision.reason

