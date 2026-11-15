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
# 依赖 / 缓存目录只出摘要，不列明细
CHANGE_GROUP = "group"

MAX_FILES = 5000
# 依赖目录最多数这么多个文件，避免把轮末卡在 node_modules 上
SKIP_COUNT_CAP = 200
# 落盘 / 传输上限：每个目录最多留 20 条明细，总量最多 200 条
PER_DIR_DETAIL = 20
MAX_STORED_ENTRIES = 200
GIT_TIMEOUT = 3.0
# 锁文件属于有意义的变化，必须单独列出，不参与目录折叠
LOCKFILES = {
    "uv.lock", "poetry.lock", "Pipfile.lock", "package-lock.json",
    "yarn.lock", "pnpm-lock.yaml", "Cargo.lock", "go.sum",
    "composer.lock", "Gemfile.lock",
}
# 扫描时直接跳过：既不属于用户改动，数量又可能极大
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".next",
    "target", ".idea", ".tox",
}
# 版本库元数据不算用户改动，连摘要都不出
VCS_DIRS = {".git", ".hg", ".svn"}

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
        groups: list[dict[str, Any]] = []
        for key in keys:
            before_state = self.before.get(key)
            after_state = after.get(key)
            if ((before_state or {}).get("code") == "IGNORED"
                    or (after_state or {}).get("code") == "IGNORED"):
                if _skip_changed(before_state, after_state):
                    state = after_state or before_state or {}
                    groups.append({
                        "path": key,
                        "change": CHANGE_GROUP,
                        "count": int(state.get("count", 0)),
                        "capped": bool(state.get("capped")),
                        "blocked": True,
                    })
                continue
            change = _classify(
                before_state, after_state, key in self.touched, self.git)
            if change:
                changes[key] = change
        if self.truncated:
            logger.warning("本轮改动扫描达到上限，清单可能不完整")
        return _compact(changes, groups)


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
        for name in list(dirs):
            if name in SKIP_DIRS:
                dirs.remove(name)
                if name in VCS_DIRS:
                    continue
                entries[os.path.join(current, name)] = _skip_summary(
                    Path(current) / name)
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


def _skip_summary(path: Path) -> dict[str, Any]:
    """依赖 / 缓存目录只数到上限，给摘要一个数量与时间指纹。"""
    count = 0
    newest = 0
    try:
        for current, _dirs, files in os.walk(path):
            for name in files:
                count += 1
                try:
                    info = os.stat(os.path.join(current, name))
                except OSError:
                    pass
                else:
                    newest = max(newest, info.st_mtime_ns)
                if count >= SKIP_COUNT_CAP:
                    return {
                        "code": "IGNORED", "exists": True, "count": count,
                        "mtime": newest, "capped": True,
                    }
    except OSError:
        pass
    return {
        "code": "IGNORED", "exists": True, "count": count,
        "mtime": newest, "capped": False,
    }


def _skip_changed(before: dict[str, Any] | None,
                  after: dict[str, Any] | None) -> bool:
    if before is None or after is None:
        return True
    return (before.get("count") != after.get("count")
            or before.get("mtime") != after.get("mtime"))


def _find_skip_dirs(root: Path, max_depth: int = 3) -> list[Path]:
    """只在浅层找依赖目录，避免为了摘要而全树遍历。"""
    found: list[Path] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if entry.name in SKIP_DIRS:
                if entry.name not in VCS_DIRS:
                    found.append(Path(entry.path))
                continue
            if depth < max_depth:
                stack.append((Path(entry.path), depth + 1))
    return found[:50]


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
            for skip_dir in _find_skip_dirs(root):
                entries[str(skip_dir.resolve())] = _skip_summary(skip_dir)
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


def _compact(changes: dict[str, str],
             blocked: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按目录压缩清单，保证单条消息落盘与传输都不会无限增长。

    每组最多保留 ``PER_DIR_DETAIL`` 条明细，全部条目共享
    ``MAX_STORED_ENTRIES`` 的预算；被截掉的只保留计数，前端显示
    “另有 X 个文件未列出”。
    """
    by_dir: dict[str, list[tuple[str, str]]] = {}
    singles: list[dict[str, Any]] = []
    for path, change in sorted(changes.items()):
        base = os.path.basename(path)
        if base in LOCKFILES:
            singles.append({"path": path, "change": change})
            continue
        by_dir.setdefault(os.path.dirname(path), []).append((path, change))

    result: list[dict[str, Any]] = list(blocked)
    budget = MAX_STORED_ENTRIES
    hidden_global = 0
    for directory in sorted(by_dir):
        items = by_dir[directory]
        if len(items) <= PER_DIR_DETAIL:
            for path, change in items:
                if budget <= 0:
                    hidden_global += 1
                    continue
                singles.append({"path": path, "change": change})
                budget -= 1
            continue
        keep = items[:PER_DIR_DETAIL]
        if budget < len(keep):
            keep = keep[:max(budget, 0)]
        budget -= len(keep)
        result.append({
            "path": directory,
            "change": CHANGE_GROUP,
            "count": len(items),
            "files": [{"path": path, "change": change} for path, change in keep],
            "hiddenCount": len(items) - len(keep),
            "blocked": False,
        })
    result.extend(singles)
    if hidden_global:
        result.append({"change": "hidden", "count": hidden_global})
    return result


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
