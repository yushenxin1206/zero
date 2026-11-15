#!/usr/bin/env python3
"""构建 dist/Zero.app：内嵌独立 CPython + 依赖 + 源码 + 启动器。

产物是未签名的 .app，用于本机双击验证。构建过程只读仓库，所有中间产物
落在 build/macos/，最终包复制到 dist/；清理这两个目录即可完整重跑。

用法：
    .venv/bin/python scripts/build_macos_app.py
    .venv/bin/python scripts/build_macos_app.py --no-compile
"""

from __future__ import annotations

import argparse
import os
import plistlib
import platform
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD_ROOT = ROOT / "build" / "macos"
APP = BUILD_ROOT / "Zero.app"
DIST = ROOT / "dist" / "Zero.app"

APP_NAME = "Zero"
BUNDLE_ID = "com.zero.desktop"
MIN_MACOS = "13.0"
PYTHON_NAME = "python3.14"

ICON_SOURCE = ROOT / "zero" / "channels" / "web" / "static" / "zero-icon.png"
LAUNCHER_SOURCE = ROOT / "packaging" / "macos" / "launcher.c"


def run(cmd: list[str], *, cwd: Path | None = None) -> None:
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=cwd, check=True)


def project_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def base_prefix() -> Path:
    """uv 管理的独立 CPython 前缀；只认可整体复制的那种构建。"""
    venv_python = ROOT / ".venv" / "bin" / "python"
    if not venv_python.exists():
        raise SystemExit("找不到 .venv/bin/python，请先 uv sync --dev")

    prefix = subprocess.check_output(
        [str(venv_python), "-c", "import sys; print(sys.base_prefix)"],
        text=True,
    ).strip()
    path = Path(prefix)
    if "uv/python" not in str(path):
        raise SystemExit(
            f"当前解释器不是 uv 管理的独立构建，无法整体内嵌：{path}")
    if not (path / "bin" / PYTHON_NAME).is_file():
        raise SystemExit(f"解释器缺少 bin/{PYTHON_NAME}：{path}")
    if not (path / "lib" / "python3.14").is_dir():
        raise SystemExit(f"解释器缺少标准库目录：{path}")
    return path


def export_requirements(destination: Path) -> None:
    run([
        "uv", "export",
        "--frozen",
        "--no-dev",
        "--extra", "web",
        "--no-emit-project",
        "--no-hashes",
        "-o", str(destination),
    ], cwd=ROOT)


def make_icns(destination: Path) -> None:
    iconset = BUILD_ROOT / "zero.iconset"
    shutil.rmtree(iconset, ignore_errors=True)
    iconset.mkdir(parents=True)
    for base in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            pixels = base * scale
            suffix = "@2x" if scale == 2 else ""
            name = f"icon_{base}x{base}{suffix}.png"
            run([
                "sips", "-z", str(pixels), str(pixels),
                str(ICON_SOURCE), "--out", str(iconset / name),
            ])
    destination.parent.mkdir(parents=True, exist_ok=True)
    run(["iconutil", "-c", "icns", str(iconset), "-o", str(destination)])


def write_info_plist(destination: Path, version: str) -> None:
    info = {
        "CFBundleDevelopmentRegion": "zh_CN",
        "CFBundleExecutable": APP_NAME,
        "CFBundleIconFile": "zero.icns",
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": "1",
        "LSApplicationCategoryType": "public.app-category.productivity",
        "LSMinimumSystemVersion": MIN_MACOS,
        "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication",
    }
    with destination.open("wb") as handle:
        plistlib.dump(info, handle, sort_keys=False)


def copy_tree(source: Path, destination: Path) -> None:
    ignore = shutil.ignore_patterns(
        "__pycache__", "*.pyc", "*.pyo", ".DS_Store", "*.egg-info")
    shutil.copytree(source, destination, ignore=ignore, symlinks=True)


def build(compile_bytecode: bool) -> None:
    if sys.platform != "darwin":
        raise SystemExit("这个脚本只能在 macOS 上构建 .app")

    for tool in ("uv", "clang", "sips", "iconutil", "ditto"):
        if shutil.which(tool) is None:
            raise SystemExit(f"缺少构建工具：{tool}")
    if not ICON_SOURCE.is_file() or not LAUNCHER_SOURCE.is_file():
        raise SystemExit("图标或启动器源码缺失")

    version = project_version()
    prefix = base_prefix()
    print(f"内嵌解释器：{prefix}")
    print(f"版本：{version}")

    shutil.rmtree(BUILD_ROOT, ignore_errors=True)
    shutil.rmtree(DIST, ignore_errors=True)
    BUILD_ROOT.mkdir(parents=True)

    contents = APP / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)

    # 1. 解释器：uv 的独立构建可整体复制，sys.prefix 会跟随新位置。
    print("复制内嵌解释器…", flush=True)
    shutil.copytree(prefix, resources / "python", symlinks=True)
    embedded_python = resources / "python" / "bin" / PYTHON_NAME

    # 2. 依赖：严格按 uv.lock 导出并装到 bundle 内的独立目录。
    print("安装运行时依赖…", flush=True)
    requirements = BUILD_ROOT / "requirements.txt"
    export_requirements(requirements)
    site_packages = resources / "site-packages"
    site_packages.mkdir()
    run([
        "uv", "pip", "install",
        "--python", str(embedded_python),
        "--system",
        "--target", str(site_packages),
        "-r", str(requirements),
    ], cwd=ROOT)
    # target 里的 bin 是依赖自带的 console script，运行期一律不用；
    # 它们的源文件 mtime 每次安装都不同，留着会让字节码不可复现。
    shutil.rmtree(site_packages / "bin", ignore_errors=True)

    # 3. 源码：static 与 skills 都是普通文件，复制即完整，无需额外资源声明。
    print("复制 zero 源码…", flush=True)
    app_dir = resources / "app"
    app_dir.mkdir()
    copy_tree(ROOT / "zero", app_dir / "zero")

    # 4. 预编译字节码；运行期由启动器禁止再写入，避免污染 bundle。
    if compile_bytecode:
        print("预编译字节码…", flush=True)
        env = os.environ.copy()
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        subprocess.run(
            [str(embedded_python), "-m", "compileall", "-q",
             str(app_dir), str(site_packages)],
            env=env, check=True)

    # 5. 图标、Info.plist、启动器。
    make_icns(resources / "zero.icns")
    write_info_plist(contents / "Info.plist", version)
    run([
        "clang", "-O2", "-Wall", "-Wextra",
        "-mmacosx-version-min=10.15",
        "-arch", platform.machine(),
        str(LAUNCHER_SOURCE),
        "-o", str(macos / APP_NAME),
    ])

    # 6. 输出：清空后 ditto，保证重复构建得到同一棵树。
    DIST.parent.mkdir(parents=True, exist_ok=True)
    run(["ditto", str(APP), str(DIST)])

    size = subprocess.check_output(
        ["du", "-sh", str(DIST)], text=True).split()[0]
    files = sum(1 for _ in DIST.rglob("*"))
    print(f"\n完成：{DIST}")
    print(f"体积：{size}，条目数：{files}")
    print("未签名（第一阶段仅本机双击验证，分发前需签名/公证）。")


def main() -> int:
    parser = argparse.ArgumentParser(description="构建未签名的 Zero.app")
    parser.add_argument(
        "--no-compile", action="store_true",
        help="跳过 compileall（仅用于快速排查，正式产物不要用）")
    args = parser.parse_args()
    build(compile_bytecode=not args.no_compile)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
