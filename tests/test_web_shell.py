"""自重启入口：预检失败要能说明原因，成功才停后端。"""

import sys

from zero.channels.web import shell


def test_launch_command_rejects_missing_entry(monkeypatch, tmp_path):
    monkeypatch.delenv("ZERO_APP_BUNDLE", raising=False)
    missing = tmp_path / "gone-zero"
    monkeypatch.setattr(shell.sys, "argv", [str(missing), "--web"])

    cmd, error = shell._launch_command()

    assert cmd == []
    assert "找不到启动脚本" in error
    assert str(missing) in error


def test_launch_command_reopens_bundle_through_launchservices(
        monkeypatch, tmp_path):
    bundle = tmp_path / "Zero.app"
    bundle.mkdir()
    monkeypatch.setenv("ZERO_APP_BUNDLE", str(bundle))
    monkeypatch.setattr(shell.sys, "argv", ["ignored", "--web"])

    cmd, error = shell._launch_command()

    assert cmd == ["/usr/bin/open", "-n", str(bundle), "--args", "--web"]
    assert error == ""


def test_launch_command_rejects_missing_bundle(monkeypatch, tmp_path):
    missing = tmp_path / "Gone.app"
    monkeypatch.setenv("ZERO_APP_BUNDLE", str(missing))
    monkeypatch.setattr(shell.sys, "argv", ["ignored"])

    cmd, error = shell._launch_command()

    assert cmd == []
    assert "找不到应用包" in error


def test_restart_api_does_not_stop_backend_on_preflight_failure(
        monkeypatch, tmp_path):
    monkeypatch.setattr(
        shell.sys, "argv", [str(tmp_path / "gone-zero")])
    holder = {}
    api = shell._BootstrapApi(holder)

    result = api.restart_app()

    assert result["ok"] is False
    assert "找不到启动脚本" in result["error"]
    # 预检失败不能把 restart 标记打上，否则窗口会直接消失
    assert "restart" not in holder


def test_restart_api_accepts_existing_entry(monkeypatch, tmp_path):
    entry = tmp_path / "zero"
    entry.write_text("", encoding="utf-8")
    monkeypatch.setattr(shell.sys, "argv", [str(entry)])
    holder = {}
    api = shell._BootstrapApi(holder)

    result = api.restart_app()

    assert result == {"ok": True, "error": ""}
    assert holder["restart"] is True
    # 没有后端 loop 时走 closed 标记，由 worker 收尾
    assert holder["closed"] is True
    assert sys.executable
