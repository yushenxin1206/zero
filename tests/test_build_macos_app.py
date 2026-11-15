"""macOS 构建脚本的架构解析与一致性校验。

真正的打包需要同架构解释器，日常测试只覆盖可离线验证的部分。
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_macos_app.py"
_spec = importlib.util.spec_from_file_location("build_macos_app", SCRIPT)
assert _spec is not None and _spec.loader is not None
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="构建脚本只在 macOS 上运行")

HOST = build.native_arch()
OTHER = "arm64" if HOST == "x86_64" else "x86_64"


def host_binary(destination: Path) -> Path:
    """复制一个属于本用户的 Mach-O，避免碰 SIP 保护的系统二进制。"""
    shutil.copy2(Path(sys.executable).resolve(), destination)
    return destination


def test_parse_args_defaults_to_none_arch():
    args = build.parse_args([])

    assert args.arch is None
    assert args.python_prefix is None
    assert args.no_compile is False


def test_parse_args_accepts_explicit_arch():
    args = build.parse_args(["--arch", OTHER, "--no-compile"])

    assert args.arch == OTHER
    assert args.no_compile is True


def test_parse_args_rejects_unknown_arch():
    with pytest.raises(SystemExit):
        build.parse_args(["--arch", "arm64e"])


def test_macho_archs_reads_host_binary():
    found = build.macho_archs(Path("/usr/bin/true"))

    assert found is not None
    assert HOST in found


def test_verify_macho_paths_accepts_host_arch(tmp_path):
    binary = tmp_path / "host.dylib"
    host_binary(binary)

    build.verify_macho_paths([binary], HOST)


def test_verify_macho_paths_rejects_other_arch(tmp_path):
    binary = tmp_path / "host.dylib"
    host_binary(binary)

    with pytest.raises(build.BuildError, match="缺少"):
        build.verify_macho_paths([binary], OTHER)


def test_verify_bundle_arch_checks_launcher_and_extensions(tmp_path):
    app = tmp_path / "Zero.app"
    resources = app / "Contents" / "Resources"
    macos = app / "Contents" / "MacOS"
    (resources / "python" / "bin").mkdir(parents=True)
    (resources / "python" / "lib").mkdir()
    (resources / "site-packages").mkdir()
    macos.mkdir(parents=True)
    for path in (
            resources / "python" / "bin" / build.PYTHON_NAME,
            macos / build.APP_NAME,
            resources / "site-packages" / "_native.so",
    ):
        host_binary(path)

    build.verify_bundle_arch(app, HOST)

    with pytest.raises(build.BuildError):
        build.verify_bundle_arch(app, OTHER)


def test_resolve_interpreter_reports_missing_arch(tmp_path):
    with pytest.raises(build.BuildError) as caught:
        build.resolve_interpreter(OTHER, search_root=tmp_path)

    message = str(caught.value)
    assert OTHER in message
    assert "uv python install" in message
    assert "python-prefix" in message


def test_validate_prefix_rejects_wrong_arch(tmp_path):
    prefix = tmp_path / "cpython"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "lib" / f"python{build.PYTHON_SERIES}").mkdir(parents=True)
    host_binary(prefix / "bin" / build.PYTHON_NAME)

    with pytest.raises(build.BuildError, match="架构"):
        build.validate_prefix(prefix, OTHER)


def test_validate_prefix_rejects_missing_stdlib(tmp_path):
    prefix = tmp_path / "cpython"
    (prefix / "bin").mkdir(parents=True)
    host_binary(prefix / "bin" / build.PYTHON_NAME)

    with pytest.raises(build.BuildError, match="标准库"):
        build.validate_prefix(prefix, HOST)


def test_install_dependencies_native_uses_interpreter(monkeypatch, tmp_path):
    captured = []

    def capture(command, *, cwd=None):
        captured.append(command)

    monkeypatch.setattr(build, "run", capture)
    site = tmp_path / "site"
    site.mkdir()

    build.install_dependencies(tmp_path, HOST, site, tmp_path / "req.txt")

    assert captured and "--python" in captured[0]
    assert "--python-platform" not in captured[0]


def test_install_dependencies_cross_uses_target_platform(
        monkeypatch, tmp_path):
    captured = []

    def capture(command, *, cwd=None):
        captured.append(command)

    monkeypatch.setattr(build, "run", capture)
    site = tmp_path / "site"
    site.mkdir()

    build.install_dependencies(tmp_path, OTHER, site, tmp_path / "req.txt")

    command = captured[0]
    assert "--python" not in command
    platform = command[command.index("--python-platform") + 1]
    assert platform == build.PAYLOAD_PLATFORMS[OTHER]
    assert command[command.index("--only-binary") + 1] == ":all:"
    assert command[command.index("--no-binary") + 1] == "proxy-tools"
