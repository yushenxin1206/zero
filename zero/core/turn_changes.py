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
# 单目录超过这个条数就只出一行“目录 + 数量”，不再逐文件展示
PER_DIR_DETAIL = 10
# 逐文件明细的总预算；超出的按父目录聚合成少量目录行
MAX_STORED_ENTRIES = 20
# 超预算聚合后最多保留的目录行数，超过就继续向父目录上提
MAX_OVERFLOW_GROUPS = 10
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
# 包管理器的安装目录：无论 virtalenv 叫什么名字，这里面的文件都只出摘要
DEP_PACKAGE_DIRS = {"site-packages", "dist-packages"}
# venv 前缀：覆盖 venv-pptx、.venv-py311 这类自定义命名
VENV_PREFIXES = ("venv", ".venv")
# 版本库元数据不算用户改动，连摘要都不出
VCS_DIRS = {".git", ".hg", ".svn"}


def _is_dep_dir_name(name: str) -> bool:
    """依赖 / 缓存目录判定：精确名单或 venv 前缀。

    ``venv``、``.venv`` 只在精确名单里，装包时常见的 ``venv-pptx``、
    ``.venv-py311`` 会漏网，因此这里补上前缀匹配。
    """
    if name in SKIP_DIRS or name in DEP_PACKAGE_DIRS:
        return True
    return name.startswith(VENV_PREFIXES)


def _dependency_root(path: str) -> str | None:
    """返回路径中第一个依赖 / 缓存目录；没有则返回 None。

    用于把散落在依赖目录里的改动并成一条摘要，而不是逐文件展示。
    """
    current = ""
    for part in Path(path).parts:
        current = os.path.join(current, part) if current else part
        if _is_dep_dir_name(part):
            return current
    return None


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
                    entry = {
                        "path": key,
                        "change": CHANGE_GROUP,
                        "count": int(state.get("count", 0)),
                        "capped": bool(state.get("capped")),
                        "blocked": True,
                    }
                    # 单文件目录不包装成“批量 1”，带上文件路径供压缩时展开
                    if state.get("file"):
                        entry["file"] = state["file"]
                    groups.append(entry)
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
    # git 未忽略的依赖目录（venv-pptx、site-packages 等）同样只出摘要；
    # 否则换到 git 工作区装包仍会把成千上万条塞进清单
    dep_entries: dict[str, dict[str, Any]] = {}
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
        dep_root = _dependency_root(str(path))
        if dep_root is not None:
            summary = dep_entries.setdefault(dep_root, {
                "code": "IGNORED", "exists": True, "count": 0,
                "mtime": 0, "capped": False,
            })
            summary["count"] = int(summary["count"]) + 1
            if summary["count"] == 1:
                summary["file"] = str(path)
            try:
                mtime = path.stat().st_mtime_ns
            except OSError:
                continue
            summary["mtime"] = max(int(summary["mtime"]), mtime)
            continue
        stat: dict[str, Any] = {"code": code, "exists": False}
        try:
            info = path.stat()
            stat.update(exists=True, size=info.st_size,
                        mtime=info.st_mtime_ns)
        except OSError:
            pass
        entries[str(path)] = stat
    entries.update(dep_entries)
    return entries


def _walk(root: Path) -> tuple[dict[str, dict[str, Any]], bool]:
    entries: dict[str, dict[str, Any]] = {}
    truncated = False
    for current, dirs, files in os.walk(root):
        for name in list(dirs):
            if _is_dep_dir_name(name):
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
    only = ""
    try:
        for current, _dirs, files in os.walk(path):
            for name in files:
                count += 1
                if count == 1:
                    only = os.path.join(current, name)
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
    summary = {
        "code": "IGNORED", "exists": True, "count": count,
        "mtime": newest, "capped": False,
    }
    # 只有一个文件时记住路径，前端直接展示文件本身而不是“批量 1”
    if count == 1:
        summary["file"] = only
    return summary


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
            if _is_dep_dir_name(entry.name):
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


# 排序用的廉价信号：常见源码扩展名优先，产物 / 二进制降权
SOURCE_EXTS = frozenset({
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue",
    ".svelte", ".go", ".rs", ".java", ".kt", ".kts", ".swift", ".c", ".h",
    ".cc", ".cpp", ".hpp", ".cs", ".rb", ".php", ".lua", ".sh", ".bash",
    ".zsh", ".sql", ".css", ".scss", ".less", ".html", ".md", ".txt",
    ".toml", ".yaml", ".yml", ".json", ".ini", ".cfg", ".env",
})
BINARY_EXTS = frozenset({
    ".pyc", ".pyo", ".so", ".dylib", ".o", ".a", ".lib", ".obj", ".exe",
    ".dll", ".class", ".jar", ".whl", ".gz", ".zip", ".tar", ".bz2", ".xz",
    ".7z", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf",
    ".mp4", ".mp3", ".woff", ".woff2", ".ttf", ".otf", ".pack", ".idx",
})


def _path_score(path: str) -> tuple[int, int, str]:
    """排序键：越像人写的文件越靠前，产物 / 依赖越靠后。

    只用两个廉价信号——扩展名与路径深度，不猜内容。
    """
    ext = os.path.splitext(path)[1].lower()
    depth = path.count(os.sep)
    score = 0
    if ext in SOURCE_EXTS:
        score += 20
    elif ext in BINARY_EXTS or not ext:
        score -= 10
    # 越深越可能是生成物或依赖内部文件
    score -= depth
    return (-score, depth, path)


def _overflow_rows(
        overflow: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """超预算的明细按父目录聚合；单文件目录直接给文件自己的路径。

    行数超过 ``MAX_OVERFLOW_GROUPS`` 时继续向父目录上提，因此不会丢条目，
    末尾也就不需要再补一条重复计数的汇总。
    """
    if not overflow:
        return []
    buckets: dict[str, dict[str, Any]] = {}
    for path, change in overflow:
        directory = os.path.dirname(path)
        entry = buckets.setdefault(
            directory, {"count": 0, "best": _path_score(path), "paths": []})
        entry["count"] += 1
        entry["best"] = min(entry["best"], _path_score(path))
        entry["paths"].append((path, change))

    rows: list[dict[str, Any]] = []
    # 只有一个文件的目录不成组，直接展示文件路径
    for directory in [key for key, entry in buckets.items()
                      if entry["count"] == 1]:
        path, change = buckets.pop(directory)["paths"][0]
        rows.append({
            "path": path, "change": change, "_score": _path_score(path),
        })

    while len(buckets) > MAX_OVERFLOW_GROUPS:
        lifted: dict[str, dict[str, Any]] = {}
        for directory, entry in buckets.items():
            parent = os.path.dirname(directory) or directory
            target = lifted.setdefault(
                parent, {"count": 0, "best": entry["best"]})
            target["count"] += entry["count"]
            target["best"] = min(target["best"], entry["best"])
        if set(lifted) == set(buckets):
            break
        buckets = lifted

    rows.extend(
        {
            "path": directory,
            "change": CHANGE_GROUP,
            "count": entry["count"],
            "files": [],
            "hiddenCount": entry["count"],
            "blocked": False,
            "_score": entry["best"],
        }
        for directory, entry in buckets.items())
    rows.sort(key=lambda item: item["_score"])
    for row in rows:
        row.pop("_score")
    return rows


def _compact(changes: dict[str, str],
             blocked: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """压缩清单：依赖并摘要、单目录超阈值出一行、明细总量限额。

    依赖目录里的改动并成一条摘要（只有一个文件时直接给文件路径）；
    单目录超过 ``PER_DIR_DETAIL`` 条只留“目录 + 数量”；逐文件明细共享
    ``MAX_STORED_ENTRIES`` 预算，超出的按父目录聚合成少量行。每个文件
    最多出现一次，因此不再额外补汇总。清单只用于展示，不会进入模型上下文。
    """
    locks: list[dict[str, Any]] = []
    deps: dict[str, list[tuple[str, str]]] = {}
    by_dir: dict[str, list[tuple[str, str]]] = {}
    for path, change in sorted(changes.items()):
        dep_root = _dependency_root(path)
        if dep_root is not None:
            deps.setdefault(dep_root, []).append((path, change))
            continue
        if os.path.basename(path) in LOCKFILES:
            locks.append({"path": path, "change": change})
            continue
        by_dir.setdefault(os.path.dirname(path), []).append((path, change))

    singles: list[tuple[str, str]] = []
    folded: list[dict[str, Any]] = []
    for directory in sorted(by_dir):
        items = by_dir[directory]
        if len(items) > PER_DIR_DETAIL:
            folded.append({
                "path": directory,
                "change": CHANGE_GROUP,
                "count": len(items),
                "files": [],
                "hiddenCount": len(items),
                "blocked": False,
                "_score": min(_path_score(path) for path, _ in items),
            })
            continue
        singles.extend(items)

    singles.sort(key=lambda item: _path_score(item[0]))
    kept = singles[:MAX_STORED_ENTRIES]
    overflow = singles[MAX_STORED_ENTRIES:]

    result: list[dict[str, Any]] = list(locks)
    result.extend({"path": path, "change": change} for path, change in kept)
    result.extend(
        {key: value for key, value in group.items() if key != "_score"}
        for group in sorted(folded, key=lambda item: item["_score"]))
    result.extend(_overflow_rows(overflow))
    for root, items in sorted(deps.items()):
        # 单个文件不包装成“批量 1”，直接给文件路径
        if len(items) == 1:
            path, change = items[0]
            result.append({"path": path, "change": change})
            continue
        result.append({
            "path": root,
            "change": CHANGE_GROUP,
            "count": len(items),
            "capped": False,
            "blocked": True,
        })
    # 依赖 / 缓存目录摘要统一放最后，避免把用户改动挤到下面
    for item in blocked:
        entry = {key: value for key, value in item.items() if key != "file"}
        if item.get("count") == 1 and item.get("file"):
            result.append({"path": item["file"], "change": CHANGE_MODIFIED})
            continue
        result.append(entry)
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
