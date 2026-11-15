"""每轮改动清单：开始/结束各取一次快照，并与工具写记录取并集。

扫描范围只取权限层给出的本轮可写根（会话工作目录 + 本轮临时写根），
不猜目录、不扫整盘。仓库内优先用 ``git status`` 找候选文件，非仓库退回
目录遍历；两种方式都带文件数上限，超限降级而不是卡住轮次。
"""

import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CHANGE_ADDED = "added"
CHANGE_MODIFIED = "modified"
CHANGE_DELETED = "deleted"

MAX_FILES = 5000
GIT_TIMEOUT = 3.0
# 扫描时直接跳过：既不属于用户改动，数量又可能极大
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".next",
    "target", ".idea", ".tox",
}

_ACTIVE: dict[tuple[str, str, str], "TurnChanges"] = {}


@dataclass
class TurnChanges:
    roots: list[Path]
    before: dict[str, dict[str, Any]] = field(default_factory=dict)
    touched: set[str] = field(default_factory=set)
    git: bool = False
    truncated: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self.roots)

    def start(self) -> None:
        if not self.enabled:
            return
        self.git = any(_is_git_root(root) for root in self.roots)
        self.before = _snapshot(self.roots, git=self.git)[0]

    def record(self, path: str) -> None:
        """工具写文件时直接记账；只收本轮可写根内的路径。"""
        if not self.enabled:
            return
        try:
            real = Path(path).resolve()
        except (OSError, RuntimeError):
            return
        if any(real.is_relative_to(root) for root in self.roots):
            self.touched.add(str(real))

    def finish(self) -> list[dict[str, str]]:
        if not self.enabled:
            return []
        after, truncated = _snapshot(self.roots, git=self.git)
        self.truncated = truncated
        changes: dict[str, str] = {}
        keys = set(self.before) | set(after) | self.touched
        for key in keys:
            change = _classify(
                self.before.get(key), after.get(key), key in self.touched,
                self.git)
            if change:
                changes[key] = change
        if self.truncated:
            logger.warning("本轮改动扫描达到上限，清单可能不完整")
        return [
            {"path": path, "change": change}
            for path, change in sorted(changes.items())
        ]


def _is_git_root(root: Path) -> bool:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _git_status(root: Path) -> dict[str, dict[str, Any]] | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "-z",
             "--untracked-files=all"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    entries: dict[str, dict[str, Any]] = {}
    tokens = proc.stdout.split("\0")
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if len(token) < 4:
            continue
        code = token[:2]
        rel = token[3:]
        if code.startswith("R") and index < len(tokens):
            # 重命名：旧路径和新路径都算改动，取新路径作为展示目标
            index += 1
        path = (root / rel).resolve()
        stat: dict[str, Any] = {"code": code, "exists": False}
        try:
            info = path.stat()
            stat.update(exists=True, size=info.st_size,
                        mtime=info.st_mtime_ns)
        except OSError:
            pass
        entries[str(path)] = stat
    return entries


def _walk(root: Path) -> tuple[dict[str, dict[str, Any]], bool]:
    entries: dict[str, dict[str, Any]] = {}
    truncated = False
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            path = os.path.join(current, name)
            try:
                info = os.stat(path)
            except OSError:
                continue
            entries[path] = {
                "code": "", "exists": True,
                "size": info.st_size, "mtime": info.st_mtime_ns,
            }
            if len(entries) >= MAX_FILES:
                return entries, True
        if len(entries) >= MAX_FILES:
            truncated = True
            break
    return entries, truncated


def _snapshot(
        roots: list[Path], *, git: bool) -> tuple[dict[str, dict[str, Any]], bool]:
    if git:
        entries: dict[str, dict[str, Any]] = {}
        for root in roots:
            if not _is_git_root(root):
                continue
            status = _git_status(root)
            if status:
                entries.update(status)
            if len(entries) >= MAX_FILES:
                return entries, True
        return entries, False
    entries = {}
    truncated = False
    for root in roots:
        walked, cut = _walk(root)
        entries.update(walked)
        truncated = truncated or cut
        if len(entries) >= MAX_FILES:
            return entries, True
    return entries, truncated


def _classify(before: dict[str, Any] | None,
              after: dict[str, Any] | None,
              touched: bool, git: bool) -> str:
    if before is None and after is None:
        return ""
    if before is None:
        assert after is not None
        if not git:
            return CHANGE_ADDED
        code = after.get("code", "")
        if "D" in code:
            return CHANGE_DELETED
        if code == "??":
            return CHANGE_ADDED
        return CHANGE_MODIFIED
    if after is None:
        if not git:
            return CHANGE_DELETED
        if before.get("exists") and not before.get("code"):
            return CHANGE_DELETED
        if not before.get("exists"):
            return CHANGE_DELETED
        return CHANGE_MODIFIED
    if not after.get("exists"):
        return CHANGE_DELETED
    if not before.get("exists"):
        return CHANGE_ADDED
    if touched:
        return CHANGE_MODIFIED
    changed = (
        before.get("size") != after.get("size")
        or before.get("mtime") != after.get("mtime")
        or before.get("code") != after.get("code"))
    return CHANGE_MODIFIED if changed else ""


def build_turn_changes(permission, tool_context) -> TurnChanges:
    """按权限层的本轮写根创建 tracker；没有权限或没有根时返回空 tracker。"""
    roots: list[Path] = []
    if permission is not None:
        try:
            envelope = permission.envelope(tool_context)
        except Exception:
            logger.debug("读取本轮写根失败", exc_info=True)
            envelope = None
        if envelope is not None:
            candidates = []
            if envelope.workspace is not None:
                candidates.append(envelope.workspace)
            candidates.extend(envelope.session_roots)
            for candidate in candidates:
                try:
                    real = Path(candidate).resolve()
                except (OSError, RuntimeError):
                    continue
                if real not in roots:
                    roots.append(real)
    return TurnChanges(roots=roots)


def register_tracker(tool_context, tracker: TurnChanges) -> None:
    _ACTIVE[_key(tool_context)] = tracker


def unregister_tracker(tool_context) -> None:
    _ACTIVE.pop(_key(tool_context), None)


def record_write(tool_context, path: str) -> None:
    """工具写文件后调用；没有活跃 tracker 时静默忽略。"""
    tracker = _ACTIVE.get(_key(tool_context)) if tool_context is not None else None
    if tracker is not None:
        tracker.record(path)


def _key(tool_context) -> tuple[str, str, str]:
    return (
        getattr(tool_context, "channel", ""),
        getattr(tool_context, "session_id", ""),
        getattr(tool_context, "turn_id", ""),
    )
