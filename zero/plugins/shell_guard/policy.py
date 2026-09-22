"""
shell 命令三态判定（纯函数，无 IO，可单测）。

判定优先级：deny > allow > ask。
- deny / allow 规则 = 用户规则（~/.zero/shell_guard.json）+ 内置规则；
  用户规则在前，优先命中。
- 匹配方式：先精确相等，再 fnmatch glob（如 "git diff*"）。
- 威胁模型：防模型手滑的护栏，不是安全沙箱（docs/shell_permission.md 第 7 节）。
"""
import fnmatch
from dataclasses import dataclass


@dataclass
class Verdict:
    tier: str = "ask"       # "allow" | "deny" | "ask"
    reason: str = ""
    rule: str = ""          # 命中的规则（ask 时为空）


# 内置禁止规则：灾难性命令（不可逆、影响系统级状态）
DEFAULT_DENY = [
    "rm -rf /", "rm -rf /*", "rm -fr /", "rm -fr /*",
    "rm -rf ~", "rm -rf ~/*", "rm -rf $HOME*",
    "sudo rm -rf /*",
    "mkfs", "mkfs *", "mkfs.* *",
    "dd of=/dev/*", "dd * of=/dev/*",
    ":(){ :|:& };:",                       # fork bomb
    "shutdown*", "poweroff*", "reboot*", "halt",
    "diskpart*",
    "format *:*",                          # Windows: format c:
    "del /s /q c:\\*", "del /f /s /q c:\\*",
    "rd /s /q c:\\*", "rmdir /s /q c:\\*",
]

# 内置允许规则：常见只读/无害命令
DEFAULT_ALLOW = [
    "pwd", "cd", "cd *",
    "ls", "ls *", "dir", "dir *",
    "echo", "echo *",
    "cat *", "type *", "head *", "tail *",
    "git status*", "git diff*", "git log*", "git show*",
    "git branch", "git remote -v",
    "python --version", "python -V",
    "pip --version", "pip list", "pip show *",
    "node --version", "npm --version",
    "whoami", "hostname", "date", "ver",
]


def normalize(command: str) -> str:
    """折叠空白，让规则匹配不受多余空格影响。"""
    return " ".join((command or "").split())


def _matches(pattern: str, command: str) -> bool:
    """精确相等优先，其次 glob。沉淀的整串命令走精确相等路径，
    即使命令里含 glob 字符（* ? [）也不会被误当成模式。"""
    return pattern == command or fnmatch.fnmatchcase(command, pattern)


def evaluate(
        command: str,
        allow_rules: list[str] | None = None,
        deny_rules: list[str] | None = None,
) -> Verdict:
    """三态判定：deny > allow > ask。用户规则优先于内置规则。"""
    cmd = normalize(command)
    if not cmd:
        return Verdict(tier="deny", reason="空命令")

    for rule in (deny_rules or []) + DEFAULT_DENY:
        if _matches(rule, cmd):
            return Verdict(
                tier="deny", rule=rule, reason=f"命中禁止规则: {rule}")

    for rule in (allow_rules or []) + DEFAULT_ALLOW:
        if _matches(rule, cmd):
            return Verdict(tier="allow", rule=rule)

    return Verdict(tier="ask", reason="命令不在白名单中，需要人工确认")
