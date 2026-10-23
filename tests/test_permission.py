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
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from zero.permission import envelope, seatbelt

from zero.bus.queue import MessageBus
from zero.core.hooks import HookContext
from zero.models import ApprovalRequest, Origin, ToolContext
from zero.permission.guard import HANDLED_TOOLS, PermissionGuard
from zero.tools.shell import Shell
from zero.tools.write import Write

DARWIN_ONLY = pytest.mark.skipif(
    sys.platform != "darwin", reason="Seatbelt / launchd 仅 macOS")


def make_ctx(channel: str = "cli", kind: str = "user") -> HookContext:
    session_id = str(uuid4())
    tc = ToolContext(channel=channel, session_id=session_id,
                     turn_id="t1", origin=Origin(kind=kind))
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


# ---------------------------------------------------------------------------
# 1. 渲染
# ---------------------------------------------------------------------------

def test_render():
    env = envelope.Envelope(project_root=Path("/tmp/proj"),
                           session_dir=Path("/tmp/proj/sess"))
    text = seatbelt.render_profile(env)
    # 渲染值一律 realpath 后的字面量（macOS：/tmp → /private/tmp）
    root = seatbelt._quote(envelope._real("/tmp/proj"))
    sess = seatbelt._quote(envelope._real("/tmp/proj/sess"))
    policy = seatbelt._quote(envelope._real(envelope.PROFILE_FILE))
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


# ---------------------------------------------------------------------------
# 2. 判定 P / G / D
# ---------------------------------------------------------------------------

def test_scope():
    v = envelope.check_readable(envelope.HOME / ".ssh" / "id_rsa")
    assert not v.ok and v.tier == envelope.D, v
    assert v.needed

    v = envelope.check_readable("zero/permission/envelope.py")
    assert v.ok and v.tier == envelope.P, v

    v = envelope.check_writable("zero/permission/envelope.py")
    assert v.ok, v

    v = envelope.check_writable("/etc/hosts")
    assert not v.ok and v.tier == envelope.D, v

    v = envelope.check_writable(outside("zero-out-selftest"))
    assert not v.ok and v.tier == envelope.G, v


# ---------------------------------------------------------------------------
# 3. 包裹
# ---------------------------------------------------------------------------

def test_wrap():
    body = "(version 1)\n(allow default)\n"
    argv, path = seatbelt.wrap(["/bin/echo", "hi"], body)
    assert argv[0] == str(seatbelt.SEATBELT_EXEC)
    assert argv[1] == "-f" and argv[2] == str(path)
    assert argv[3:] == ["/bin/echo", "hi"]
    assert path.read_text(encoding="utf-8") == body
    path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 4. 探针
# ---------------------------------------------------------------------------

def test_probe():
    if sys.platform != "darwin":
        assert not envelope.available()
        return
    assert envelope.available(), envelope.unavailable_reason()


# ---------------------------------------------------------------------------
# 5 / 6. guard 档位
# ---------------------------------------------------------------------------

def test_guard_basics():
    async def main():
        bus = MessageBus()
        guard = PermissionGuard(bus)

        assert await guard("read", {}, make_ctx()) is None, "非 shell 应放行"

        if envelope.available():
            assert await guard("shell", {"command": "ls -la"},
                               make_ctx()) is None, "P 档应放行"

        # 无人值守 + 需扩权 → fail-closed，且不进面板
        ctx = make_ctx(kind="scheduler")
        worker = answer_next(bus, "s")
        d = await guard("shell", {"command": "echo x",
                                  "write_paths": [outside("zero-out-selftest")]},
                        ctx)
        assert d is not None and d.proceed is False, d
        assert "无人值守" in d.reason, d.reason
        await asyncio.sleep(0.05)
        assert not worker.done(), "无人值守不应进面板"
        worker.cancel()

    asyncio.run(main())


def test_guard_unattended_expand_fail_closed():
    """非 CLI 渠道同样无人值守。"""
    async def main():
        bus = MessageBus()
        guard = PermissionGuard(bus)
        ctx = make_ctx(channel="feishu")
        d = await guard("shell", {"command": "echo x",
                                  "write_paths": [outside("zero-out-selftest")]},
                        ctx)
        assert d is not None and d.proceed is False, d

    asyncio.run(main())


def test_guard_tiers():
    if not envelope.available():
        pytest.skip("内核沙箱不可用，跳过 D/G 档语义")

    async def main():
        bus = MessageBus()
        guard = PermissionGuard(bus)

        # D 档：不可授予 → 拒绝，且绝不进面板
        ctx = make_ctx()
        worker = answer_next(bus, "s")
        d = await guard("shell", {"command": "echo x",
                                  "write_paths": ["/etc/hosts"]}, ctx)
        assert d is not None and d.proceed is False, d
        assert "不可授予" in d.reason, d.reason
        await asyncio.sleep(0.05)
        assert not worker.done(), "D 档不应进面板"
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
        assert envelope.session_roots(ctx), "「本会话允许」应写入会话扩权表"
        assert envelope.check_writable(target, ctx).ok, "扩权后该路径应可写"
        envelope.reset_session(ctx)

        # G 档 + 拒绝 → 拒绝，且不改状态
        ctx = make_ctx()
        target2 = outside("zero-out-selftest-b")
        worker = answer_next(bus, "n")
        d = await guard("shell", {"command": "echo x",
                                  "write_paths": [target2]}, ctx)
        await worker
        assert d is not None and d.proceed is False, d
        assert not envelope.check_writable(target2, ctx).ok

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 7. shell.py 回投
# ---------------------------------------------------------------------------

@DARWIN_ONLY
def test_shell_denied_roundtrip():
    if not envelope.available():
        pytest.skip("内核沙箱不可用，跳过 shell 拒绝回投")

    shell = Shell()
    ctx = ToolContext(channel="cli", session_id=str(uuid4()))

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


# ---------------------------------------------------------------------------
# 8. launchd 变更面：读允许 / 写拒绝（报警线）
# ---------------------------------------------------------------------------

@DARWIN_ONLY
def test_launchd_mutation_blocked():
    """锁住「launchd 只能读、不能变」这一语义。

    实测（本机 macOS）：沙箱内 `launchctl print gui/<uid>` 成功——读走的是每个
    进程都继承的 bootstrap port；而 `bootstrap` / `submit` 被拒——写需要
    privileged bootstrap right。这个区分**不是我们设计的**，macOS 升级一旦翻转
    它没有任何东西会报警，而翻转即意味着沙箱能被"让 launchd 替我跑沙箱外进程"
    完整绕过。本测试就是那道报警线。

    对照步（先跑沙箱外）是刻意的：证明该向量本身真实存在，避免"沙箱内失败"
    是因为命令压根不可用而变成空测试。
    """
    if not envelope.available():
        pytest.skip("内核沙箱不可用，跳过 launchd 变更面")

    uid = os.getuid()
    tag = uuid4().hex[:8]
    label = f"com.example.zero-permission-probe.{tag}"
    plist = Path(f"zero-permission-probe-{tag}.plist")
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

    text = seatbelt.render_profile(envelope.current_envelope(None))

    def sandboxed(argv):
        wrapped, profile = seatbelt.wrap(argv, text)
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

def test_file_tools_use_approval_gate():
    """read/write/edit 越界不再只是 fail-closed，而是走审批门。

    读方向是减法（全开减不可授予），所以只有 D 档、永不进面板；
    写方向才有 G 档，审批通过后落会话扩权表，同会话内后续同路径免审批。
    """
    async def main():
        bus = MessageBus()
        guard = PermissionGuard(bus)

        # read 到凭证目录：D 档拒绝，不进面板
        ctx = make_ctx()
        worker = answer_next(bus, "s")
        d = await guard("read",
                        {"file_path": str(envelope.HOME / ".ssh" / "id_rsa")}, ctx)
        assert d is not None and d.proceed is False, d
        assert "不可授予" in d.reason, d.reason
        await asyncio.sleep(0.05)
        assert not worker.done(), "读方向 D 档不应进面板"
        worker.cancel()

        # read 包络内：放行，不弹面板
        worker = answer_next(bus, "s")
        assert await guard("read", {"file_path": "zero/permission/envelope.py"},
                           make_ctx()) is None
        await asyncio.sleep(0.05)
        assert not worker.done(), "包络内不应进面板"
        worker.cancel()

        # write 到系统路径：D 档拒绝，不进面板
        ctx = make_ctx()
        worker = answer_next(bus, "s")
        d = await guard("write", {"file_path": "/etc/zero-stage-b-test",
                                  "content": "x"}, ctx)
        assert d is not None and d.proceed is False, d
        assert "不可授予" in d.reason, d.reason
        await asyncio.sleep(0.05)
        assert not worker.done(), "写方向 D 档不应进面板"
        worker.cancel()

        # write 越界 + 无人值守：fail-closed
        d = await guard("write", {"file_path": outside("zero-stageB-unattended.txt"),
                                  "content": "x"}, make_ctx(kind="scheduler"))
        assert d is not None and d.proceed is False, d
        assert "无人值守" in d.reason, d.reason

        # write 包络内：放行，不弹面板
        worker = answer_next(bus, "s")
        assert await guard("write", {"file_path": "zero-stageB-inside.txt",
                                     "content": "x"}, make_ctx()) is None
        await asyncio.sleep(0.05)
        assert not worker.done(), "包络内不应进面板"
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
        assert envelope.check_writable(str(target), ctx).ok, "授权后该路径应可写"
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
        assert not envelope.check_writable(target2, ctx).ok

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 10 / 11. 覆盖与失败语义
# ---------------------------------------------------------------------------

def test_guard_covers_all_file_tools():
    """漏接检测：注册表里任何带 *_path 参数的工具都必须在 HANDLED_TOOLS 里。

    守卫按工具名分发，所以"新增一个文件类工具却忘了登记"不会报错，只会**静默
    失去检查**。这条测试就是那道报警线。
    """
    from zero.main import build_tool_registry

    registry = build_tool_registry()
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


def test_guard_fail_closed_on_internal_error(monkeypatch):
    """守卫内部异常必须变成拒绝，不能被 hook 的 fail-open 吞成放行。"""
    def boom(*_a, **_kw):
        raise RuntimeError("injected guard bug")

    monkeypatch.setattr(envelope, "check_readable", boom)
    guard = PermissionGuard(MessageBus())
    decision = asyncio.run(
        guard("read", {"file_path": "zero/permission/envelope.py"}, make_ctx()))

    assert decision is not None and decision.proceed is False, decision
    assert "fail-closed" in decision.reason, decision.reason

