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
from pathlib import Path


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

# 内置 skill 脚本放行：随包分发的 zero/skills/<skill>/scripts/*.py 是可信资源，
# 执行它们免审批（plan skill 靠它落盘，每步都弹审批会让"实时跟踪"失效）。
# 收紧两点：首 token 必须是 python 解释器；脚本路径必须是绝对路径且真实落在
# 包内 skills 目录下。威胁模型仍是"防模型手滑"，不是沙箱。
_SKILLS_ROOT = Path(__file__).resolve().parents[2] / "skills"
_PYTHON_NAMES = {
    "python", "python3", "python.exe", "python3.exe", "py", "py.exe"}


def _tokenize(command: str) -> list[str]:
    """按空白切分并剥离外层引号，但**不**解释反斜杠转义。

    不能用 shlex：Windows 路径含反斜杠，POSIX 语义会把 `C:\\a\\b.py`
    的转义吃掉，脚本路径无法还原。
    """
    tokens: list[str] = []
    cur: list[str] = []
    quote = ""
    for ch in command:
        if quote:
            if ch == quote:
                quote = ""
            else:
                cur.append(ch)
        elif ch in ("'", '"'):
            quote = ch
        elif ch.isspace():
            if cur:
                tokens.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        tokens.append("".join(cur))
    return tokens


def is_builtin_skill_script(command: str) -> bool:
    """命令是否为「python 解释器 + 包内 <skill>/scripts/*.py 绝对路径」。

    相对路径无法在当前 cwd 下可靠判定归属，一律不放行（退回 ask）；
    非 .py（如 `cat skill.md`）同样不放行。
    """
    tokens = _tokenize(command)
    if len(tokens) < 2:
        return False
    if Path(tokens[0]).name.lower() not in _PYTHON_NAMES:
        return False
    root = _SKILLS_ROOT.resolve()
    for tok in tokens[1:]:
        p = Path(tok)
        if not p.is_absolute() or p.suffix.lower() != ".py":
            continue
        try:
            rel = p.resolve().relative_to(root)
        except (ValueError, OSError):
            continue
        # 必须直接位于 <skill>/scripts/ 下
        if len(rel.parts) >= 3 and rel.parts[-2] == "scripts":
            return True
    return False


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

    # 内置 skill 脚本（包内可信资源）免审批；deny 优先，故置于其后。
    # 用未折叠空白的原命令，避免 normalize 破坏含空格路径的还原。
    if is_builtin_skill_script(command):
        return Verdict(tier="allow", rule="builtin-skill-script")

    for rule in (allow_rules or []) + DEFAULT_ALLOW:
        if _matches(rule, cmd):
            return Verdict(tier="allow", rule=rule)

    return Verdict(tier="ask", reason="命令不在白名单中，需要人工确认")
