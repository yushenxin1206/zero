"""
macOS 内核沙箱后端（Seatbelt / sandbox-exec）。

只做平台相关的三件事：把包络渲染成 SBPL、把 argv 包进 sandbox-exec、为自检
提供一次性执行。

**本模块不 import ``envelope``**——包络以 duck typing 传入（需要
``deny_read()`` / ``write_roots()`` / ``network`` 三个成员），因此依赖方向是
``envelope → seatbelt`` 单向，不会成环。

非 macOS 上本模块的逻辑仍可调用，只是 ``envelope.available()`` 会返回 False。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from zero.paths import current

logger = logging.getLogger(__name__)

SEATBELT_EXEC = Path("/usr/bin/sandbox-exec")
PROBE_MARK = ".seatbelt-probe"  # 探针标记，落在 profiles 目录内（不污染用户仓库）


def _profiles_dir() -> Path:
    """沙箱 profile 的临时目录，随当前数据根结算（不 import 时冻结）。"""
    return current().profiles


class SeatbeltError(Exception):
    """profile 渲染/包裹失败。一律 fail-closed，不允许静默降级。"""


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _quote(path: Path) -> str:
    s = str(path)
    if any(c in s for c in ('"', "\\", "\n")):
        raise SeatbeltError(f"路径含无法安全写入 profile 的字符：{s!r}")
    return f'"{s}"'


def _probe_mark() -> Path:
    """探针标记的真实路径。恒定为 deny 项，是"deny 真的生效"的判据。"""
    return Path(os.path.realpath(_profiles_dir() / PROBE_MARK))


def render_profile(env: Any) -> str:
    """包络 → SBPL 文本。字面量插值，cat 出来即最终语义，可直接审计。"""
    out = [
        "(version 1)",
        "(deny default)",
        "",
        "; 基线：dyld / 系统 mach 服务。import 不可用时探针会失败 → 降级。",
        '(import "system.sb")',
        "",
        "(allow process-fork)",
        "(allow process-exec)",
        "(allow signal (target self))",
        "(allow sysctl-read)",
        "; 凭证服务必须显式 deny：mach 可达 = 钥匙串服务可达，",
        "; 光 deny 钥匙串文件（~/Library/Keychains）挡不住服务层请求。",
        "; 这里不裸放 mach-lookup：实测 system.sb 的基线名单",
        "; 已足够跑 git / curl / clang / make / python，裸放只会白开 securityd。",
        "(deny mach-lookup",
        '  (global-name "com.apple.SecurityServer")',
        '  (global-name "com.apple.securityd")',
        ")",
        "(allow file-write-data",
        '  (literal "/dev/null")',
        '  (literal "/dev/dtracehelper")',
        ")",
        "",
        "; ---- 读：全开，再抠掉凭证与策略文件 ----",
        "(allow file-read*)",
        "(deny file-read*",
    ]
    for p in env.deny_read():
        out.append(f"  (subpath {_quote(p)})")
    out.append(f"  (subpath {_quote(_probe_mark())})")
    out.append(")")
    out.append("")

    roots = env.write_roots()
    if roots:
        out.append("; ---- 写：纯加法白名单，其余由 deny default 兜底 ----")
        out.append("(allow file-write*")
        for r in roots:
            out.append(f"  (subpath {_quote(r)})")
        out.append(")")
        out.append("")

    if env.network:
        out.append("(allow network*)")
    else:
        # 无人值守来源（scheduler / subagent / 非交互渠道）：显式 deny，
        # 压过 system.sb 基线里可能存在的网络放行。
        out.append("; ---- 网络：无人值守来源，显式禁止 ----")
        out.append("(deny network*)")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# 包裹
# ---------------------------------------------------------------------------

def wrap(argv: list[str], profile_text: str) -> tuple[list[str], Path]:
    """把 argv 包进 sandbox-exec。返回 (新 argv, profile 路径)，调用方负责 unlink。

    profiles 目录 0700 且不在写集合内 → 沙箱内进程改不了它（deny default 兜底）。
    不加 /bin/sh -c，保持"无 shell 语义"不变。
    """
    profiles = _profiles_dir()
    profiles.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(profiles, 0o700)
    except OSError:  # noqa: BLE001 - 权限收紧失败不应阻断执行
        logger.warning("无法收紧 %s 权限", profiles)
    fd, name = tempfile.mkstemp(suffix=".sb", dir=str(profiles))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(profile_text)
    return [str(SEATBELT_EXEC), "-f", name, *argv], Path(name)


def run_isolated(profile_text: str,
                 argv: list[str]) -> subprocess.CompletedProcess:
    """在给定 profile 下一次性执行 argv（自检探针用）。profile 用完即删。"""
    path: Path | None = None
    try:
        wrapped, path = wrap(argv, profile_text)
        return subprocess.run(wrapped, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:  # noqa: BLE001
        logger.warning("探针执行失败 %s：%s", argv, e)
        return subprocess.CompletedProcess(argv, 1, b"", str(e).encode())
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 平台闸门
# ---------------------------------------------------------------------------

def supported_platform() -> bool:
    return sys.platform == "darwin"


def executable_present() -> bool:
    return SEATBELT_EXEC.exists()
