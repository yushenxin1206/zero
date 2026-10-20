"""
能力包络与内核沙箱。

本模块是唯一持有包络的地方：判定、profile 渲染、包裹、探测都在这里。
策略文件 ``~/.zero/profile.json`` 是用户资产，模型不可写（写它属于 D 档）。

三个执行档位——判定看**资源**，不看命令文本：
    P  包络内    → 沙箱内直接跑，免审批
    G  可授予    → 需审批（面板：本次 / 本会话 / 总是 / 拒绝）
    D  不可授予  → 永不进面板，结构化拒绝

两个执行平面：
    子进程平面（shell）：内核 Seatbelt 强制，``wrap()`` 把 argv 包进 sandbox-exec
    进程内平面（read/write/edit）：应用层护栏，``check_readable`` / ``check_writable``

``available()`` 不通过时**不许自称"沙箱内免审批"**，降级为每次审批。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from zero.config import HOME, ZERO_ROOT
from zero.session.store import SessionStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 档位
# ---------------------------------------------------------------------------

P = "P"  # 包络内：免审批
G = "G"  # 可授予：审批
D = "D"  # 不可授予：拒绝，无面板

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
PROFILE_FILE = ZERO_ROOT / "profile.json"
PROFILES_DIR = ZERO_ROOT / "run"
PROBE_MARK = ".sandbox-probe"   # 探针标记文件，落在 PROFILES_DIR 内（不污染用户仓库）

# 不可授予：凭证与策略文件。读方向是减法，这份名单恒定渲染进 profile。
UNGRANTABLE_READ: tuple[Path, ...] = (
    HOME / ".ssh",
    HOME / ".aws",
    HOME / ".gnupg",
    HOME / ".config" / "gh",
    HOME / ".netrc",
    HOME / "Library" / "Keychains",
    PROFILE_FILE,
    ZERO_ROOT / "config.json",
    ZERO_ROOT / "scheduler",
)

# 不可授予：系统路径与策略文件。写方向是加法，这份名单用于挡住"审批扩权"到系统区。
UNGRANTABLE_WRITE: tuple[Path, ...] = (
    Path("/etc"),
    Path("/System"),
    Path("/usr"),
    Path("/bin"),
    Path("/sbin"),
    Path("/Library"),
    Path("/dev"),
    PROFILE_FILE,
    ZERO_ROOT / "config.json",
    ZERO_ROOT / "scheduler",
)


class SandboxError(Exception):
    """包络渲染/包裹失败。一律 fail-closed，不允许静默降级。"""


# ---------------------------------------------------------------------------
# 路径工具
# ---------------------------------------------------------------------------

def _real(path) -> Path:
    """realpath + expanduser。SBPL 只认真实路径，Python 侧统一预解析。"""
    return Path(os.path.realpath(os.path.expanduser(str(path))))


def project_root() -> Path:
    """工作区根 = agent 进程启动目录。这是包络的写集合中心。

    注意：不接受工具参数指定——否则模型能靠改 cwd 自行扩大包络。
    """
    return _real(Path.cwd())


# ---------------------------------------------------------------------------
# 包络
# ---------------------------------------------------------------------------

def _tmp_dirs() -> list[Path]:
    """TMPDIR 不重定向，只放行写（npm / pip / cargo 都靠它）。"""
    dirs = [Path("/private/tmp")]
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        dirs.append(Path(tmpdir))
    return dirs


@dataclass(frozen=True)
class Envelope:
    """一次执行的能力包络。读 = 全开减去不可授予；写 = 白名单相加。"""
    project_root: Path
    session_dir: Path | None = None
    extra_roots: tuple[Path, ...] = ()      # profile.json 的长期扩权根（用户资产）
    session_roots: tuple[Path, ...] = ()    # 审批"本会话"后的临时写根
    network: bool = True

    def write_roots(self) -> list[Path]:
        candidates = [self.project_root]
        if self.session_dir is not None:
            candidates.append(self.session_dir)
        candidates.extend(_tmp_dirs())
        candidates.extend(self.extra_roots)
        candidates.extend(self.session_roots)
        roots: list[Path] = []
        seen: set[Path] = set()
        for c in candidates:
            r = _real(c)
            if r not in seen:
                seen.add(r)
                roots.append(r)
        return roots


def _quote(path: Path) -> str:
    s = str(path)
    if any(c in s for c in ('"', "\\", "\n")):
        raise SandboxError(f"路径含无法安全写入 profile 的字符：{s!r}")
    return f'"{s}"'


def render_profile(env: Envelope) -> str:
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
    for p in UNGRANTABLE_READ:
        out.append(f"  (subpath {_quote(_real(p))})")
    out.append(f"  (subpath {_quote(_real(PROFILES_DIR / PROBE_MARK))})")
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
    return "\n".join(out) + "\n"


def wrap(argv: list[str], profile_text: str) -> tuple[list[str], Path]:
    """把 argv 包进 sandbox-exec。返回 (新 argv, profile 路径)，调用方负责 unlink。

    profiles 目录 0700 且不在写集合内 → 沙箱内进程改不了它（deny default 兜底）。
    不加 /bin/sh -c，保持"无 shell 语义"不变。
    """
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(PROFILES_DIR, 0o700)
    except OSError:  # noqa: BLE001 - 权限收紧失败不应阻断执行
        logger.warning("无法收紧 %s 权限", PROFILES_DIR)
    fd, name = tempfile.mkstemp(suffix=".sb", dir=str(PROFILES_DIR))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(profile_text)
    return [str(SANDBOX_EXEC), "-f", name, *argv], Path(name)


# ---------------------------------------------------------------------------
# 能力探测（fail-closed 闸门）
# ---------------------------------------------------------------------------

_probe_cache: dict[str, bool] = {}


def unavailable_reason(root: Path | None = None) -> str:
    if sys.platform != "darwin":
        return "非 macOS，无 Seatbelt"
    if not SANDBOX_EXEC.exists():
        return f"{SANDBOX_EXEC} 不存在"
    return "自检探针未通过（基线或 deny 任一侧失败）"


def available(root: Path | None = None) -> bool:
    """内核沙箱是否真的可用：平台 + sandbox-exec + 双向探针。

    不通过就不许自称"沙箱内免审批"，降级为每次审批。
    """
    if sys.platform != "darwin" or not SANDBOX_EXEC.exists():
        return False
    root = _real(root or project_root())
    key = str(root)
    if key not in _probe_cache:
        ok = _probe(root)
        _probe_cache[key] = ok
        logger.info("sandbox 探针 %s：%s", root, "通过" if ok else "未通过")
    return _probe_cache[key]


def _probe(root: Path) -> bool:
    """双向探针：既确认 deny 真的生效，也确认基线没把正常命令误杀。"""
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    mark = PROFILES_DIR / PROBE_MARK
    try:
        mark.write_text("probe", encoding="utf-8")
    except OSError as e:  # noqa: BLE001
        logger.warning("探针标记不可写 %s：%s", mark, e)
        return False
    try:
        text = render_profile(Envelope(project_root=root, session_dir=root))
    except SandboxError as e:
        logger.warning("探针 profile 渲染失败：%s", e)
        return False
    if _run_probe(text, ["/bin/echo", "ok"]).returncode != 0:
        return False  # 基线或 import 不可用
    if _run_probe(text, ["/bin/cat", str(mark)]).returncode == 0:
        return False  # deny 未生效
    return True


def _run_probe(profile_text: str, argv: list[str]) -> subprocess.CompletedProcess:
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
# profile.json（用户资产；模型不可写）
# ---------------------------------------------------------------------------

def load_profile() -> dict:
    try:
        data = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:  # noqa: BLE001
        logger.warning("profile.json 读取失败，按空包络处理：%s", e)
        return {}
    return data if isinstance(data, dict) else {}


def save_profile(data: dict) -> None:
    """只由 CLI 与审批的「总是允许」调用。"""
    PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def get_extra_roots() -> list[Path]:
    return [_real(p) for p in load_profile().get("extraRoots", [])
            if isinstance(p, str)]


def add_extra_root(path) -> Path:
    """长期扩权根落盘。跨会话、跨渠道永久生效。"""
    p = _real(path)
    data = load_profile()
    roots = data.setdefault("extraRoots", [])
    if str(p) not in roots:
        roots.append(str(p))
        save_profile(data)
    return p


def get_unsandbox() -> list[str]:
    """脱离沙箱白名单：这些命令无法在沙箱内跑（如 brew），命中即强制审批。"""
    return [n for n in load_profile().get("unsandbox", []) if isinstance(n, str)]


def add_unsandbox(name: str) -> None:
    data = load_profile()
    names = data.setdefault("unsandbox", [])
    if name not in names:
        names.append(name)
        save_profile(data)


def matches_unsandbox(argv: list[str]) -> bool:
    """按首个 token 的 basename 精确匹配——不做 glob 引擎。"""
    if not argv:
        return False
    return Path(argv[0]).name in get_unsandbox()


# ---------------------------------------------------------------------------
# 会话级扩权（审批"本会话"）
# ---------------------------------------------------------------------------

_session_roots: dict[tuple[str, str], set[Path]] = {}


def _key(ctx) -> tuple[str, str]:
    return (getattr(ctx, "channel", "") or "", getattr(ctx, "session_id", "") or "")


def grant_write_root(path, ctx, scope: str) -> None:
    """登记审批结果。scope: "session" 本会话有效；"always" 落 profile.json。"""
    if scope == "always":
        add_extra_root(path)
    elif scope == "session":
        _session_roots.setdefault(_key(ctx), set()).add(_real(path))


def session_roots(ctx) -> tuple[Path, ...]:
    return tuple(sorted(_session_roots.get(_key(ctx), set()), key=str))


def reset_session(ctx) -> None:
    _session_roots.pop(_key(ctx), None)


def current_envelope(ctx=None) -> Envelope:
    session_dir = None
    if ctx is not None and getattr(ctx, "session_id", ""):
        try:
            session_dir = SessionStore.session_dir(ctx.channel, ctx.session_id)
        except Exception:  # noqa: BLE001 - 推导失败退化为项目根，不阻断
            logger.warning("会话目录推导失败，退化为项目根", exc_info=True)
    return Envelope(
        project_root=project_root(),
        session_dir=session_dir,
        extra_roots=tuple(get_extra_roots()),
        session_roots=session_roots(ctx),
        network=True,
    )


# ---------------------------------------------------------------------------
# 应用层硬边界（进程内工具的护栏）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Verdict:
    ok: bool
    tier: str = P
    needed: str = ""     # 缺失能力的机器可读描述，用于回投 hint
    reason: str = ""


def _under(path: Path, root: Path) -> bool:
    return path.is_relative_to(root)


def check_readable(path, ctx=None) -> Verdict:
    """读方向是减法：除不可授予名单外都可读。"""
    p = _real(path)
    for bad in UNGRANTABLE_READ:
        if _under(p, _real(bad)):
            return Verdict(
                ok=False, tier=D, needed=f"fs.read {p}",
                reason=f"{p} 属于不可授予的读能力（凭证或策略文件），换路径。")
    return Verdict(ok=True)


def check_writable(path, ctx=None) -> Verdict:
    """写方向是加法：必须在写集合内，且不落在不可授予名单。"""
    p = _real(path)
    for bad in UNGRANTABLE_WRITE:
        if _under(p, _real(bad)):
            return Verdict(
                ok=False, tier=D, needed=f"fs.write {p}",
                reason=f"{p} 属于不可授予的写能力（系统路径或策略文件），换路径。")
    if any(_under(p, r) for r in current_envelope(ctx).write_roots()):
        return Verdict(ok=True)
    return Verdict(
        ok=False, tier=G, needed=f"fs.write {p}",
        reason=(f"{p} 不在当前写集合内（可写：项目根、会话目录、TMPDIR）。"
                "改用包络内路径。"))


# ---------------------------------------------------------------------------
# CLI：zero-sandbox
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "check"

    if cmd == "check":
        ok = available()
        print(f"内核沙箱：{'可用' if ok else '不可用'}")
        if not ok:
            print(f"原因：{unavailable_reason()}")
            print("影响：shell 命令一律走审批，没有「沙箱内免审批」")
        return 0

    if cmd == "list":
        env = current_envelope(None)
        print("写集合（加法白名单）:")
        for r in env.write_roots():
            print(f"  {r}")
        print(f"网络：{'放行' if env.network else '禁止'}")
        print("脱离沙箱白名单：" + ("、".join(get_unsandbox()) or "（空）"))
        print("长期扩权根：" + ("、".join(str(p) for p in get_extra_roots()) or "（空）"))
        print("不可授予（写）：" + "、".join(str(_real(p)) for p in UNGRANTABLE_WRITE))
        return 0

    if cmd == "add-root" and len(args) > 1:
        p = add_extra_root(args[1])
        print(f"已加入长期可写根：{p}")
        return 0

    if cmd == "add-unsandbox" and len(args) > 1:
        add_unsandbox(args[1])
        print(f"已加入脱离沙箱白名单（命中强制审批）：{args[1]}")
        return 0

    print(__doc__.split("三个执行档位")[0].strip())
    print("\n用法：\n"
          "  zero-sandbox check                 探针结论与降级原因\n"
          "  zero-sandbox list                  当前写集合、网络、扩权集\n"
          "  zero-sandbox add-root <path>       追加长期可写根\n"
          "  zero-sandbox add-unsandbox <name>  追加脱离沙箱白名单（强制审批）")
    return 2

if __name__ == "__main__":
    sys.exit(main())
