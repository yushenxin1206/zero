"""从聊天界面打开网址与本地路径。

边界很窄：只做"打开 / 在文件管理器里定位"，从不执行内容。

- 网址只接受 http / https，其它协议（file:、javascript: 等）一律拒绝。
- 本地路径必须是绝对路径、真实存在，且不接受 ``..`` 这类越权构造。
- 可执行文件与脚本不交给系统"打开"（那可能等于运行），改为在文件管理器中定位。
"""

import logging
import os
import subprocess
import sys
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

URL_SCHEMES = frozenset({"http", "https"})
MAX_TARGET_LENGTH = 4096

# 交给系统打开就等于运行的东西：一律只定位、不打开
_ALWAYS_REVEAL_SUFFIXES = frozenset({
    ".app", ".command", ".appimage", ".desktop", ".run",
    ".exe", ".com", ".bat", ".cmd", ".msi", ".scr",
})
# Windows 上这些后缀会被 shell 当程序执行（os.startfile 会直接跑）
_WINDOWS_REVEAL_SUFFIXES = frozenset({
    ".py", ".pyw", ".js", ".jse", ".vbs", ".vbe",
    ".ps1", ".reg", ".wsf", ".hta", ".lnk",
})


def open_target(kind: str, value: str) -> dict[str, object]:
    """打开一个目标；返回 ``{"ok": bool, "error": str}``。"""
    if kind == "url":
        url, error = _validate_url(value)
        if error:
            return {"ok": False, "error": error}
        try:
            _dispatch_url(url)
        except (OSError, subprocess.SubprocessError):
            logger.warning("failed to open url: %s", url, exc_info=True)
            return {"ok": False, "error": "无法调用系统浏览器"}
        return {"ok": True, "error": ""}

    if kind == "path":
        path, error = _resolve_path(value)
        if error:
            return {"ok": False, "error": error}
        try:
            _dispatch_path(path)
        except (OSError, subprocess.SubprocessError):
            logger.warning("failed to open path: %s", path, exc_info=True)
            return {"ok": False, "error": "无法打开该路径"}
        return {"ok": True, "error": ""}

    return {"ok": False, "error": "不支持的打开类型"}


def _validate_url(raw: str) -> tuple[str, str]:
    url = str(raw or "").strip()
    if not url:
        return "", "网址为空"
    if len(url) > MAX_TARGET_LENGTH:
        return "", "网址过长"
    if any(ch in url for ch in "\x00\r\n\t"):
        return "", "网址包含非法字符"
    try:
        parts = urlsplit(url)
    except ValueError:
        return "", "网址格式不正确"
    if parts.scheme.lower() not in URL_SCHEMES:
        return "", "只支持 http / https 网址"
    if not parts.netloc:
        return "", "网址缺少主机名"
    return url, ""


def _resolve_path(raw: str) -> tuple[Path | None, str]:
    text = str(raw or "").strip()
    if not text:
        return None, "路径为空"
    if len(text) > MAX_TARGET_LENGTH:
        return None, "路径过长"
    if any(ch in text for ch in "\x00\r\n"):
        return None, "路径包含非法字符"
    if "://" in text:
        return None, "路径格式不正确"
    if text.startswith("~"):
        text = os.path.expanduser(text)
    candidate = Path(text)
    if not candidate.is_absolute():
        return None, "只支持绝对路径"
    # 规范化之前先拒绝显式上跳，避免被拼出预期之外的目录
    if ".." in Path(text).parts:
        return None, "路径不允许包含 .."
    try:
        real = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None, "路径不存在"
    return real, ""


def _is_executable(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix in _ALWAYS_REVEAL_SUFFIXES:
        return True
    if os.name == "nt" and suffix in _WINDOWS_REVEAL_SUFFIXES:
        return True
    # POSIX 上带执行位的文件也不交给 open，避免像 .app/脚本那样被运行
    return os.name != "nt" and os.access(path, os.X_OK)


def _dispatch_url(url: str) -> None:
    if sys.platform == "darwin":
        _run(["/usr/bin/open", url])
    elif os.name == "nt":
        startfile = getattr(os, "startfile", None)
        if startfile is None:  # pragma: no cover - Windows 一定有
            raise OSError("no startfile")
        startfile(url)
    elif not webbrowser.open(url, new=2):
        raise OSError("no browser")


def _dispatch_path(path: Path) -> None:
    if sys.platform == "darwin":
        _run(_mac_command(path))
    elif os.name == "nt":
        if path.is_dir() or _is_executable(path):
            subprocess.Popen(
                ["explorer", f"/select,{path}"], **_popen_kwargs())
        else:
            startfile = getattr(os, "startfile", None)
            if startfile is None:  # pragma: no cover - Windows 一定有
                raise OSError("no startfile")
            startfile(str(path))
    else:
        target = path.parent if _is_executable(path) and not path.is_dir() else path
        _run(["xdg-open", str(target)])


def _mac_command(path: Path) -> list[str]:
    """目录与可执行文件只定位（Finder 选中），普通文件才真正打开。"""
    reveal = path.is_dir() or _is_executable(path)
    return ["/usr/bin/open", *(["-R"] if reveal else []), str(path)]


def _popen_kwargs() -> dict:
    return {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }


def _run(cmd: list[str]) -> None:
    result = subprocess.run(
        cmd, timeout=10, **_popen_kwargs())
    if result.returncode != 0:
        raise OSError(f"command failed: {cmd[0]}")
