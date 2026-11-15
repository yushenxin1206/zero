#!/usr/bin/env python3
"""构建 Zero.app：内嵌独立 CPython + 依赖 + 源码 + 启动器。

默认产出本机架构；``--arch`` 显式指定 ``x86_64`` 或 ``arm64``。目标架构没有
可用的同架构解释器时直接报错，绝不产出混架构的包。产物在
``dist/<arch>/Zero.app``，中间目录在 ``build/macos/<arch>/``。

用法：
    .venv/bin/python scripts/build_macos_app.py
    .venv/bin/python scripts/build_macos_app.py --arch arm64
    .venv/bin/python scripts/build_macos_app.py --arch arm64 --python-prefix /path/to/cpython
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
ARCHS = ("x86_64", "arm64")
PYTHON_SERIES = "3.14"
PYTHON_NAME = f"python{PYTHON_SERIES}"
PAYLOAD_PLATFORMS = {
    "x86_64": "x86_64-apple-darwin",
    "arm64": "aarch64-apple-darwin",
}
UV_PYTHON_TAGS = {"x86_64": "x86_64", "arm64": "aarch64"}
# 这些依赖只有源码包，但都是纯 Python（构建出 py3-none-any），
# 交叉构建时放行，不需要本机编译原生代码。
PURE_SDIST_PACKAGES = ("proxy-tools",)

APP_NAME = "Zero"
BUNDLE_ID = "com.zero.desktop"
MIN_MACOS = "13.0"

ICON_SOURCE = ROOT / "zero" / "channels" / "web" / "static" / "zero-icon.png"
LAUNCHER_SOURCE = ROOT / "packaging" / "macos" / "launcher.c"


class BuildError(RuntimeError):
    """构建前置条件不满足，或产物与目标架构不一致。"""


def run(cmd: list[str], *, cwd: Path | None = None) -> None:
    print("+ " + " ".join(cmd), flush=True)
    try:
        subprocess.run(cmd, cwd=cwd, check=True)
    except subprocess.CalledProcessError as error:
        raise BuildError(f"命令失败（退出码 {error.returncode}）：{cmd[0]}") from error


def project_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def native_arch() -> str:
    machine = platform.machine()
    if machine in ARCHS:
        return machine
    raise BuildError(f"不支持的构建机架构：{machine}")


def uv_python_root() -> Path:
    try:
        output = subprocess.check_output(
            ["uv", "python", "dir"], text=True).strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise BuildError("无法定位 uv 的解释器目录，请确认 uv 已安装") from error
    return Path(output)


def macho_archs(path: Path) -> frozenset[str] | None:
    """返回 Mach-O 包含的架构；非 Mach-O 文件返回 None。"""
    result = subprocess.run(
        ["lipo", "-archs", str(path)], capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return frozenset(result.stdout.split())


def require_arch(path: Path, arch: str, description: str) -> None:
    found = macho_archs(path)
    if found is None:
        raise BuildError(f"{description} 不是 Mach-O 文件：{path}")
    if arch not in found:
        actual = "/".join(sorted(found)) or "未知"
        raise BuildError(
            f"{description} 缺少 {arch} 架构切片（实际 {actual}）：{path}")


def verify_macho_paths(paths: list[Path], arch: str) -> None:
    """每个 Mach-O 都必须包含目标架构；universal2 允许保留其他切片。"""
    mismatched: list[str] = []
    for path in paths:
        found = macho_archs(path)
        if found is None or arch in found:
            continue
        actual = "/".join(sorted(found)) or "未知"
        mismatched.append(f"{path}（{actual}）")
    if mismatched:
        preview = "\n".join(f"  - {item}" for item in mismatched[:10])
        more = "" if len(mismatched) <= 10 else f"\n  …共 {len(mismatched)} 个"
        raise BuildError(
            f"产物里存在缺少 {arch} 架构的 Mach-O：\n{preview}{more}")


def validate_prefix(path: Path, arch: str) -> Path:
    path = path.expanduser().resolve()
    python = path / "bin" / PYTHON_NAME
    if not python.is_file():
        raise BuildError(f"解释器前缀缺少 bin/{PYTHON_NAME}：{path}")
    if not (path / "lib" / f"python{PYTHON_SERIES}").is_dir():
        raise BuildError(
            f"解释器前缀缺少标准库目录 lib/python{PYTHON_SERIES}：{path}")
    require_arch(python, arch, "解释器")
    return path


def resolve_interpreter(
        arch: str,
        override: Path | None = None,
        *,
        search_root: Path | None = None,
) -> Path:
    """找到目标架构的独立 CPython；宁可直接失败，也不猜。"""
    if override is not None:
        return validate_prefix(override, arch)

    root = search_root or uv_python_root()
    tag = UV_PYTHON_TAGS[arch]
    pattern = f"cpython-{PYTHON_SERIES}.*-macos-{tag}-none"
    candidates = sorted(
        (item for item in root.glob(pattern) if item.is_dir()),
        reverse=True,
    )
    for candidate in candidates:
        try:
            return validate_prefix(candidate, arch)
        except BuildError:
            continue

    # 本机 .venv 的基解释器也算是现成来源，但必须架构一致。
    venv_python = ROOT / ".venv" / "bin" / "python"
    if venv_python.exists():
        base = Path(subprocess.check_output(
            [str(venv_python), "-c", "import sys; print(sys.base_prefix)"],
            text=True,
        ).strip())
        try:
            return validate_prefix(base, arch)
        except BuildError:
            pass

    raise BuildError(
        f"找不到 {arch} 架构的独立 CPython {PYTHON_SERIES}。\n"
        f"请在一台 {arch} 的 Mac 上执行 `uv python install {PYTHON_SERIES}`，"
        f"或用 `--python-prefix` 指向一个 {arch} 的解释器前缀。\n"
        f"已检查目录：{root}\n"
        "不能用其他架构的解释器凑数——那会产出无法启动或需要 Rosetta 的包。")


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


def install_dependencies(
        prefix: Path, arch: str, site_packages: Path,
        requirements: Path) -> None:
    if arch == native_arch():
        command = [
            "uv", "pip", "install",
            "--python", str(prefix / "bin" / PYTHON_NAME),
            "--system",
            "--target", str(site_packages),
            "-r", str(requirements),
        ]
    else:
        # 异架构解释器无法在本机执行，改用 uv 的交叉解析；
        # 只接受 wheel，避免在本机编译出错误的架构。少数纯 Python 包
        # 只有源码包，单独放行。
        command = [
            "uv", "pip", "install",
            "--target", str(site_packages),
            "-r", str(requirements),
            "--python-platform", PAYLOAD_PLATFORMS[arch],
            "--python-version", PYTHON_SERIES,
            "--only-binary", ":all:",
        ]
        for package in PURE_SDIST_PACKAGES:
            command += ["--no-binary", package]
    run(command, cwd=ROOT)
    # target 里的 bin 是依赖自带的 console script，运行期一律不用；
    # 它们的源文件 mtime 每次安装都不同，留着会让字节码不可复现。
    shutil.rmtree(site_packages / "bin", ignore_errors=True)


def make_icns(destination: Path, build_root: Path) -> None:
    iconset = build_root / "zero.iconset"
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


def verify_bundle_arch(app: Path, arch: str) -> None:
    resources = app / "Contents" / "Resources"
    interpreter = resources / "python"
    checks = [
        interpreter / "bin" / PYTHON_NAME,
        app / "Contents" / "MacOS" / APP_NAME,
    ]
    libpython = interpreter / "lib" / f"libpython{PYTHON_SERIES}.dylib"
    if libpython.exists():
        checks.append(libpython)
    for binary in checks:
        require_arch(binary, arch, "产物中的可执行文件")

    extension_roots = [interpreter, resources / "site-packages"]
    binaries = [
        path for root in extension_roots
        for pattern in ("*.so", "*.dylib")
        for path in root.rglob(pattern)
        if path.is_file()
    ]
    verify_macho_paths(binaries, arch)


def build(
        arch: str,
        python_prefix: Path | None = None,
        *,
        compile_bytecode: bool = True,
) -> Path:
    if sys.platform != "darwin":
        raise BuildError("这个脚本只能在 macOS 上构建 .app")
    if arch not in ARCHS:
        raise BuildError(f"不支持的架构：{arch}")

    for tool in ("uv", "clang", "lipo", "sips", "iconutil", "ditto"):
        if shutil.which(tool) is None:
            raise BuildError(f"缺少构建工具：{tool}")
    if not ICON_SOURCE.is_file() or not LAUNCHER_SOURCE.is_file():
        raise BuildError("图标或启动器源码缺失")

    version = project_version()
    prefix = resolve_interpreter(arch, python_prefix)
    print(f"目标架构：{arch}")
    print(f"内嵌解释器：{prefix}")

    build_root = ROOT / "build" / "macos" / arch
    app = build_root / "Zero.app"
    dist = ROOT / "dist" / arch / "Zero.app"
    shutil.rmtree(build_root, ignore_errors=True)
    shutil.rmtree(dist.parent, ignore_errors=True)
    build_root.mkdir(parents=True)

    contents = app / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)

    # 1. 解释器：uv 的独立构建可整体复制，sys.prefix 会跟随新位置。
    print("复制内嵌解释器…", flush=True)
    shutil.copytree(prefix, resources / "python", symlinks=True)

    # 2. 依赖：严格按 uv.lock 导出并装到 bundle 内的独立目录。
    print("安装运行时依赖…", flush=True)
    requirements = build_root / "requirements.txt"
    export_requirements(requirements)
    site_packages = resources / "site-packages"
    site_packages.mkdir()
    install_dependencies(prefix, arch, site_packages, requirements)

    # 3. 源码：static 与 skills 都是普通文件，复制即完整。
    print("复制 zero 源码…", flush=True)
    app_dir = resources / "app"
    app_dir.mkdir()
    copy_tree(ROOT / "zero", app_dir / "zero")

    # 4. 预编译字节码；运行期由启动器禁止再写入，避免污染 bundle。
    #    字节码与 CPU 架构无关，异架构构建可以用宿主解释器代编。
    if compile_bytecode:
        if arch == native_arch():
            compiler = resources / "python" / "bin" / PYTHON_NAME
        elif sys.version_info[:2] == (3, 14):
            compiler = Path(sys.executable)
        else:
            compiler = None
            print("  跳过预编译：宿主解释器不是 3.14，无法代编", flush=True)
        if compiler is not None:
            print("预编译字节码…", flush=True)
            env = os.environ.copy()
            env.pop("PYTHONDONTWRITEBYTECODE", None)
            subprocess.run(
                [str(compiler), "-m", "compileall", "-q",
                 str(app_dir), str(site_packages)],
                env=env, check=True)

    # 5. 图标、Info.plist、启动器。
    make_icns(resources / "zero.icns", build_root)
    write_info_plist(contents / "Info.plist", version)
    run([
        "clang", "-O2", "-Wall", "-Wextra",
        "-mmacosx-version-min=10.15",
        "-arch", arch,
        str(LAUNCHER_SOURCE),
        "-o", str(macos / APP_NAME),
    ])

    # 6. 先校验再落盘：架构不一致的包不进入 dist。
    print("校验产物架构…", flush=True)
    verify_bundle_arch(app, arch)

    # 7. 输出：清空后 ditto，保证重复构建得到同一棵树。
    dist.parent.mkdir(parents=True, exist_ok=True)
    run(["ditto", str(app), str(dist)])

    size = subprocess.check_output(
        ["du", "-sh", str(dist)], text=True).split()[0]
    files = sum(1 for _ in dist.rglob("*"))
    print(f"\n完成：{dist}")
    print(f"体积：{size}，条目数：{files}")
    if arch != native_arch():
        print("注意：本机无法运行异架构产物，请到对应架构的 Mac 上验收。")
    print("未签名（第一阶段仅本机双击验证，分发前需签名/公证）。")
    return dist


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建未签名的 Zero.app")
    parser.add_argument(
        "--arch", choices=ARCHS, default=None,
        help="目标架构；默认取构建机架构")
    parser.add_argument(
        "--python-prefix", type=Path, default=None,
        help="指定内嵌 CPython 前缀（必须与 --arch 一致）")
    parser.add_argument(
        "--no-compile", action="store_true",
        help="跳过 compileall（仅用于快速排查，正式产物不要用）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    arch = args.arch or native_arch()
    try:
        build(
            arch,
            args.python_prefix,
            compile_bytecode=not args.no_compile,
        )
    except BuildError as error:
        print(f"构建失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
