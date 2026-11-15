"""聊天链接打开：协议 / 路径校验，以及"只打开不执行"的分派规则。"""

import pytest

from zero.channels.web import opener


def test_url_accepts_http_and_dispatches(monkeypatch):
    seen = []
    monkeypatch.setattr(opener, "_dispatch_url", seen.append)

    result = opener.open_target("url", "https://example.com/a?b=1")

    assert result == {"ok": True, "error": ""}
    assert seen == ["https://example.com/a?b=1"]


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "javascript:alert(1)",
    "ftp://example.com/x",
    "data:text/html,hi",
    "https://",
    "https:///path",
])
def test_url_rejects_non_http_or_malformed(monkeypatch, url):
    seen = []
    monkeypatch.setattr(opener, "_dispatch_url", seen.append)

    result = opener.open_target("url", url)

    assert result["ok"] is False
    assert seen == []


def test_url_rejects_control_characters(monkeypatch):
    seen = []
    monkeypatch.setattr(opener, "_dispatch_url", seen.append)

    result = opener.open_target("url", "https://example.com/\nrm -rf /")

    assert result["ok"] is False
    assert seen == []


def test_path_resolves_existing_file(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("hi", encoding="utf-8")

    path, error = opener._resolve_path(str(target))

    assert error == ""
    assert path == target.resolve()


@pytest.mark.parametrize("raw", [
    "relative/note.txt",
    "~/note.txt",
    "/tmp/../tmp",
    "/definitely/not/here-zero.txt",
    "file:///tmp/x",
    "/tmp/bad\x00name",
])
def test_path_rejects_relative_traversal_and_missing(raw):
    path, error = opener._resolve_path(raw)

    assert path is None
    assert error


def test_open_path_dispatches_real_target(monkeypatch, tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("hi", encoding="utf-8")
    seen = []
    monkeypatch.setattr(opener, "_dispatch_path", seen.append)

    result = opener.open_target("path", str(target))

    assert result == {"ok": True, "error": ""}
    assert seen == [target.resolve()]


def test_unknown_kind_rejected():
    assert opener.open_target("command", "rm -rf /")["ok"] is False


def test_mac_open_reveals_directories_and_scripts(tmp_path):
    folder = tmp_path / "dir"
    folder.mkdir()
    script = tmp_path / "run.command"
    script.write_text("echo hi", encoding="utf-8")
    plain = tmp_path / "note.txt"
    plain.write_text("hi", encoding="utf-8")

    assert opener._mac_command(folder)[1] == "-R"
    assert opener._mac_command(script)[1] == "-R"
    assert opener._mac_command(plain)[1] == str(plain)


def test_executable_bit_is_revealed_not_opened(tmp_path):
    target = tmp_path / "tool"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    target.chmod(0o755)

    assert opener._is_executable(target) is True


def test_windows_script_suffixes_are_not_opened(monkeypatch, tmp_path):
    target = tmp_path / "run.py"
    target.write_text("print(1)", encoding="utf-8")
    monkeypatch.setattr(opener.os, "name", "nt")

    assert opener._is_executable(target) is True


def test_linux_reveals_executable_through_parent(monkeypatch, tmp_path):
    target = tmp_path / "run.command"
    target.write_text("echo hi", encoding="utf-8")
    seen = []
    monkeypatch.setattr(opener, "_run", seen.append)
    monkeypatch.setattr(opener.sys, "platform", "linux")

    opener._dispatch_path(target)

    assert seen == [["xdg-open", str(tmp_path)]]


def test_dispatch_never_uses_shell(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs

        class Result:
            returncode = 0

        return Result()

    monkeypatch.setattr(opener.subprocess, "run", fake_run)
    monkeypatch.setattr(opener.sys, "platform", "darwin")

    opener._dispatch_url("https://example.com")

    assert isinstance(seen["cmd"], list)
    assert seen["kwargs"].get("shell") is None


def test_shell_api_delegates_to_opener(monkeypatch):
    from zero.channels.web import shell

    seen = []
    monkeypatch.setattr(
        shell.opener, "open_target",
        lambda kind, value: seen.append((kind, value)) or {"ok": True})

    api = shell._BootstrapApi({})
    assert api.open_target("path", "/tmp") == {"ok": True}
    assert seen == [("path", "/tmp")]


def test_access_check_uses_resolved_path(tmp_path):
    target = (tmp_path / "a").resolve()
    target.mkdir()
    alias = tmp_path / "link"
    alias.symlink_to(target)

    path, error = opener._resolve_path(str(alias))

    assert error == ""
    assert path == target


def test_missing_symlink_rejected(tmp_path):
    alias = tmp_path / "broken"
    alias.symlink_to(tmp_path / "gone")

    path, error = opener._resolve_path(str(alias))

    assert path is None
    assert "不存在" in error
