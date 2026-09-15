"""
shell_guard 用户规则存储：~/.zero/shell_guard.json

文件只存用户规则（审批时沉淀的 + zero-guard 手动添加的）；
内置规则在 policy.py，不落盘。格式：

    {"allow": ["git fetch*", ...], "deny": ["rm -rf *", ...]}
"""
import json
from pathlib import Path

STORE_PATH = Path.home() / ".zero" / "shell_guard.json"

_KINDS = ("allow", "deny")


def load() -> dict[str, list[str]]:
    """读取用户规则；文件缺失或损坏时返回空规则。"""
    data = {k: [] for k in _KINDS}
    if not STORE_PATH.exists():
        return data
    try:
        raw = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return data
    for kind in _KINDS:
        rules = raw.get(kind)
        if isinstance(rules, list):
            data[kind] = [str(r) for r in rules]
    return data


def save(data: dict[str, list[str]]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def rules() -> tuple[list[str], list[str]]:
    """返回 (allow_rules, deny_rules)。"""
    data = load()
    return data["allow"], data["deny"]


def add(kind: str, pattern: str) -> bool:
    """添加规则；已存在则返回 False。"""
    if kind not in _KINDS:
        raise ValueError(f"kind 必须是 {_KINDS} 之一")
    pattern = pattern.strip()
    if not pattern:
        raise ValueError("规则不能为空")
    data = load()
    if pattern in data[kind]:
        return False
    data[kind].append(pattern)
    save(data)
    return True


def remove(kind: str, index: int) -> str | None:
    """按序号（0 起）删除规则，返回被删的规则；越界返回 None。"""
    if kind not in _KINDS:
        raise ValueError(f"kind 必须是 {_KINDS} 之一")
    data = load()
    if not 0 <= index < len(data[kind]):
        return None
    removed = data[kind].pop(index)
    save(data)
    return removed
