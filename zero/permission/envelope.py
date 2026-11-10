"""
能力包络与判定（跨平台）。

``PermissionState`` 持有权限层的全部显式状态：数据根、会话存储、能力探针
缓存、会话扩权表，以及策略文件 ``<data root>/profile.json``（用户资产，模型
不可写；写它属于 D 档）。由组合根创建并显式注入守卫、Shell 与 Context；
包络推导不再读进程级路径。

三个执行档位——判定看**资源**，不看命令文本：
    P  包络内    → 免审批
    G  可授予    → 需审批（面板：本会话 / 总是 / 拒绝）
    D  不可授予  → 永不进面板，结构化拒绝

平台相关的部分（SBPL 渲染、sandbox-exec 包裹、自检执行）在 ``seatbelt``
后端里；本模块通过 ``PermissionState.sandbox_available()`` 向它提问。非 macOS
上这里的逻辑照常成立，只是 ``sandbox_available()`` 为 False，于是没有
「包络内免审批」。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from zero.config import HOME
from zero.data_paths import DataPaths
from zero.permission import seatbelt
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

# 有人值守、可以就地弹审批面板作答的渠道。网络放行与 G 档审批共用这个
# 判定：无人值守来源既不能弹面板，也不允许自动联网。
ATTENDED_CHANNELS = frozenset({"cli", "web"})


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


def _origin_of(ctx):
    """从 ToolContext 或 HookContext 取本轮来源。"""
    origin = getattr(ctx, "origin", None)
    if origin is None:
        origin = getattr(getattr(ctx, "tool_context", None), "origin", None)
    return origin


def attended(ctx) -> bool:
    """本轮是否有人值守且由用户触发。

    接受 ``ToolContext`` 或 ``HookContext``；``None``（裸查询）按无人值守处理。
    scheduler / subagent 是无人值守来源，飞书等非交互渠道同样无人值守。
    这个判定同时决定「能否弹审批」与「网络是否放行」——两者是同一个人在场
    才能成立的能力，拆成两套规则必然漂移。
    """
    if ctx is None or getattr(ctx, "channel", "") not in ATTENDED_CHANNELS:
        return False
    origin = _origin_of(ctx)
    return origin is None or getattr(origin, "kind", "user") == "user"


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
    deny_read: tuple[Path, ...] = ()        # 读禁止项（已 realpath）
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


@dataclass(frozen=True)
class Verdict:
    ok: bool
    tier: str = P
    needed: str = ""     # 缺失能力的机器可读描述，用于回投 hint
    reason: str = ""


def _under(path: Path, root: Path) -> bool:
    return path.is_relative_to(root)


class PermissionState:
    """权限层的显式状态与操作集合。

    由组合根按同一组 ``DataPaths`` / ``SessionStore`` 构造一次，再注入守卫、
    Shell 与 Context。探针缓存与会话扩权表都是进程内可变状态，必须随对象
    走（而不是挂在模块上），否则多组合根会互相串权。
    """

    def __init__(self, data_paths: DataPaths, session_store: SessionStore):
        self.data_paths = data_paths
        self.session_store = session_store
        # 能力探针结果按工作区根缓存：同一次运行内重复询问不重复跑子进程
        self._probe_cache: dict[str, bool] = {}
        self._network_probe_cache: dict[str, bool] = {}
        # 会话扩权表按「数据根 + 渠道 + 会话」隔离，避免同 id 跨根串权
        self._session_roots: dict[tuple[str, str, str], set[Path]] = {}

    # ------------------------------------------------------------------
    # 不可授予名单（策略文件路径由注入的 DataPaths 提供）
    # ------------------------------------------------------------------

    def ungrantable_read(self) -> tuple[Path, ...]:
        """不可授予：凭证与策略文件。读方向是减法，这份名单是唯一读禁止集合。"""
        p = self.data_paths
        return (
            HOME / ".ssh",
            HOME / ".aws",
            HOME / ".gnupg",
            HOME / ".config" / "gh",
            HOME / ".netrc",
            HOME / "Library" / "Keychains",
            p.profile_file,
            p.config,
            p.scheduler,
        )

    def ungrantable_write(self) -> tuple[Path, ...]:
        """不可授予：系统路径与策略文件。写方向是加法，用于挡住审批扩权到系统区。"""
        p = self.data_paths
        return (
            Path("/etc"),
            Path("/System"),
            Path("/usr"),
            Path("/bin"),
            Path("/sbin"),
            Path("/Library"),
            Path("/dev"),
            p.profile_file,
            p.config,
            p.scheduler,
        )

    # ------------------------------------------------------------------
    # 能力探测（fail-closed 闸门）—— 向 seatbelt 后端确认「内核强制真的在跑」
    # ------------------------------------------------------------------

    def sandbox_unavailable_reason(self) -> str:
        if not seatbelt.supported_platform():
            return "非 macOS，无 Seatbelt"
        if not seatbelt.executable_present():
            return f"{seatbelt.SEATBELT_EXEC} 不存在"
        return "自检探针未通过（基线或 deny 任一侧失败）"

    def sandbox_available(self, root: Path | None = None) -> bool:
        """内核沙箱是否真的可用：平台 + sandbox-exec + 双向探针。

        不通过就不许自称"沙箱内免审批"，降级为每次审批。
        """
        if not seatbelt.supported_platform() or not seatbelt.executable_present():
            return False
        root = _real(root or project_root())
        key = str(root)
        if key not in self._probe_cache:
            ok = self._probe(root)
            self._probe_cache[key] = ok
            logger.debug("内核沙箱探针 %s：%s", root, "通过" if ok else "未通过")
        return self._probe_cache[key]

    def network_enforced(self, root: Path | None = None) -> bool:
        """断网档是否真的生效（双向探针，进程内缓存）。

        对照步在宿主上执行同一连接，必须成功——证明监听口与解释器可用，
        避免"命令本身跑不起来"被误读成"网络被拒"。
        """
        if not seatbelt.supported_platform() or not seatbelt.executable_present():
            return False
        root = _real(root or project_root())
        key = str(root)
        if key not in self._network_probe_cache:
            ok = self._probe_network(root)
            self._network_probe_cache[key] = ok
            logger.debug("断网探针 %s：%s", root, "通过" if ok else "未通过")
        return self._network_probe_cache[key]

    def _probe(self, root: Path) -> bool:
        """双向探针：既确认 deny 真的生效，也确认基线没把正常命令误杀。"""
        profiles = self.data_paths.profiles
        profiles.mkdir(parents=True, exist_ok=True)
        mark = profiles / seatbelt.PROBE_MARK
        try:
            mark.write_text("probe", encoding="utf-8")
        except OSError as e:  # noqa: BLE001
            logger.warning("探针标记不可写 %s：%s", mark, e)
            return False
        try:
            text = seatbelt.render_profile(
                Envelope(project_root=root, session_dir=root), profiles)
        except seatbelt.SeatbeltError as e:
            logger.warning("探针 profile 渲染失败：%s", e)
            return False
        if seatbelt.run_isolated(
                text, ["/bin/echo", "ok"], profiles).returncode != 0:
            return False  # 基线或 import 不可用
        if seatbelt.run_isolated(
                text, ["/bin/cat", str(mark)], profiles).returncode == 0:
            return False  # deny 未生效
        return True

    def _probe_network(self, root: Path) -> bool:
        """双向探针：宿主能连上本地监听口，同一命令进沙箱后必须连不上。"""
        import socket

        profiles = self.data_paths.profiles
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen(5)          # 对照步的连接会留在 backlog，留足余量
            port = listener.getsockname()[1]
            code = (
                "import socket\n"
                f"socket.create_connection(('127.0.0.1', {port}), 2)\n"
            )
            argv = [sys.executable, "-B", "-c", code]
            try:
                control = subprocess.run(argv, capture_output=True, timeout=10)
            except (OSError, subprocess.SubprocessError) as e:  # noqa: BLE001
                logger.warning("断网探针对照步失败：%s", e)
                return False
            if control.returncode != 0:
                return False
            text = seatbelt.render_profile(
                Envelope(project_root=root, session_dir=root, network=False),
                profiles)
            denied = seatbelt.run_isolated(text, argv, profiles)
            # sandbox-exec 自身失败不能当成"连接被拒"，否则探针会假阳性。
            if b"sandbox-exec:" in denied.stderr:
                logger.warning("断网探针沙箱步执行失败：%s", denied.stderr[:200])
                return False
            return denied.returncode != 0
        finally:
            listener.close()

    # ------------------------------------------------------------------
    # 沙箱执行（把 profiles 目录的所有权收在本对象内）
    # ------------------------------------------------------------------

    def render_profile(self, env: Envelope) -> str:
        return seatbelt.render_profile(env, self.data_paths.profiles)

    def wrap(self, argv: list[str],
             profile_text: str) -> tuple[list[str], Path]:
        return seatbelt.wrap(argv, profile_text, self.data_paths.profiles)

    # ------------------------------------------------------------------
    # profile.json（用户资产；模型不可写）
    # ------------------------------------------------------------------

    def load_profile(self) -> dict:
        try:
            data = json.loads(
                self.data_paths.profile_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:  # noqa: BLE001
            logger.warning("profile.json 读取失败，按空包络处理：%s", e)
            return {}
        return data if isinstance(data, dict) else {}

    def save_profile(self, data: dict) -> None:
        """只由 CLI 与审批的「总是允许」调用。"""
        path = self.data_paths.profile_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")

    def extra_roots(self) -> list[Path]:
        return [_real(p) for p in self.load_profile().get("extraRoots", [])
                if isinstance(p, str)]

    def add_extra_root(self, path) -> Path:
        """长期扩权根落盘。跨会话、跨渠道永久生效。"""
        p = _real(path)
        data = self.load_profile()
        roots = data.setdefault("extraRoots", [])
        if str(p) not in roots:
            roots.append(str(p))
            self.save_profile(data)
        return p

    def unsandbox_names(self) -> list[str]:
        """脱离沙箱白名单：这些命令无法在沙箱内跑（如 brew），命中即强制审批。"""
        return [n for n in self.load_profile().get("unsandbox", [])
                if isinstance(n, str)]

    def add_unsandbox(self, name: str) -> None:
        data = self.load_profile()
        names = data.setdefault("unsandbox", [])
        if name not in names:
            names.append(name)
            self.save_profile(data)

    def matches_unsandbox(self, argv: list[str]) -> bool:
        """按首个 token 的 basename 精确匹配——不做 glob 引擎。"""
        if not argv:
            return False
        return Path(argv[0]).name in self.unsandbox_names()

    # ------------------------------------------------------------------
    # 会话级扩权（审批"本会话"）
    # ------------------------------------------------------------------

    def _key(self, ctx) -> tuple[str, str, str]:
        return (
            str(self.data_paths.root),
            getattr(ctx, "channel", "") or "",
            getattr(ctx, "session_id", "") or "",
        )

    def grant_write_root(self, path, ctx, scope: str) -> None:
        """登记审批结果。scope: "session" 本会话有效；"always" 落 profile.json。"""
        if scope == "always":
            self.add_extra_root(path)
        elif scope == "session":
            self._session_roots.setdefault(self._key(ctx), set()).add(_real(path))

    def session_roots(self, ctx) -> tuple[Path, ...]:
        return tuple(
            sorted(self._session_roots.get(self._key(ctx), set()), key=str))

    def reset_session(self, ctx) -> None:
        self._session_roots.pop(self._key(ctx), None)

    # ------------------------------------------------------------------
    # 包络推导与判定
    # ------------------------------------------------------------------

    def envelope(self, ctx=None) -> Envelope:
        session_dir = None
        if ctx is not None and getattr(ctx, "session_id", ""):
            try:
                session_dir = self.session_store.session_dir(
                    ctx.channel, ctx.session_id)
            except Exception:  # noqa: BLE001 - 推导失败退化为项目根，不阻断
                logger.warning(
                    "会话目录推导失败，退化为项目根", exc_info=True)
        return Envelope(
            project_root=project_root(),
            session_dir=session_dir,
            deny_read=tuple(_real(p) for p in self.ungrantable_read()),
            extra_roots=tuple(self.extra_roots()),
            session_roots=self.session_roots(ctx),
            network=attended(ctx),
        )

    def check_readable(self, path, ctx=None) -> Verdict:
        """读方向是减法：除不可授予名单外都可读。"""
        p = _real(path)
        for bad in self.ungrantable_read():
            if _under(p, _real(bad)):
                return Verdict(
                    ok=False, tier=D, needed=f"fs.read {p}",
                    reason=f"{p} 属于不可授予的读能力（凭证或策略文件），换路径。")
        return Verdict(ok=True)

    def check_writable(self, path, ctx=None) -> Verdict:
        """写方向是加法：必须在写集合内，且不落在不可授予名单。"""
        p = _real(path)
        for bad in self.ungrantable_write():
            if _under(p, _real(bad)):
                return Verdict(
                    ok=False, tier=D, needed=f"fs.write {p}",
                    reason=f"{p} 属于不可授予的写能力（系统路径或策略文件），换路径。")
        if any(_under(p, r) for r in self.envelope(ctx).write_roots()):
            return Verdict(ok=True)
        return Verdict(
            ok=False, tier=G, needed=f"fs.write {p}",
            reason=(f"{p} 不在当前写集合内（可写：项目根、会话目录、TMPDIR）。"
                    "改用包络内路径。"))


# ---------------------------------------------------------------------------
# CLI：zero-permission
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None, *,
         data_paths: DataPaths | None = None) -> int:
    """``zero-permission`` CLI。

    ``main`` 是进程边界：未注入 ``data_paths`` 时按 ``DataPaths.default()``
    构造；测试/嵌入方传入隔离根即可，无需改 HOME。
    """
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "check"

    if data_paths is None:
        data_paths = DataPaths.default()
    state = PermissionState(
        data_paths, SessionStore(data_paths.sessions))

    if cmd == "check":
        ok = state.sandbox_available()
        print(f"内核沙箱：{'可用' if ok else '不可用'}")
        if not ok:
            print(f"原因：{state.sandbox_unavailable_reason()}")
            print("影响：shell 命令一律走审批，没有「沙箱内免审批」")
        else:
            net = state.network_enforced()
            print(f"断网档：{'生效' if net else '探针未通过'}")
        return 0

    if cmd == "list":
        env = state.envelope()
        print("写集合（加法白名单）:")
        for r in env.write_roots():
            print(f"  {r}")
        print("网络：交互来源（cli/web 的用户轮次）放行；"
              "无人值守来源（scheduler / subagent / 其它渠道）显式禁止")
        print("脱离沙箱白名单：" + ("、".join(state.unsandbox_names()) or "（空）"))
        print("长期扩权根：" + (
            "、".join(str(p) for p in state.extra_roots()) or "（空）"))
        print("不可授予（写）：" + "、".join(
            str(_real(p)) for p in state.ungrantable_write()))
        return 0

    if cmd == "add-root" and len(args) > 1:
        p = state.add_extra_root(args[1])
        print(f"已加入长期可写根：{p}")
        return 0

    if cmd == "add-unsandbox" and len(args) > 1:
        state.add_unsandbox(args[1])
        print(f"已加入脱离沙箱白名单（命中强制审批）：{args[1]}")
        return 0

    print(__doc__.split("三个执行档位")[0].strip())
    print("\n用法：\n"
          "  zero-permission check                 探针结论与降级原因\n"
          "  zero-permission list                  当前写集合、网络、扩权集\n"
          "  zero-permission add-root <path>       追加长期可写根\n"
          "  zero-permission add-unsandbox <name>  追加脱离沙箱白名单（强制审批）")
    return 2

if __name__ == "__main__":
    sys.exit(main())
