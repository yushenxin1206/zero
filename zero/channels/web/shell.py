"""
pywebview desktop shell for the web channel.

Threading model:
  - pywebview's GUI loop owns the main thread and must stay there.
  - The asyncio backend (MessageBus, AgentLoop, WebSocket server) runs in
    the worker thread started by ``webview.start(func=...)``.
  - JS talks to Python only once, through ``bootstrap()``, to learn the
    WebSocket URL + token. Everything else flows over the socket.

Closing the window sets the channel's stop event on the backend loop;
the backend then cancels its tasks and returns, which lets ``webview.start``
finish and the process exit.
"""
import asyncio
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

STATIC_PATH = Path(__file__).with_name("static") / "index.html"

THEMES = ("light", "dark")
DEFAULT_THEME = "light"


def _read_theme(path: Path | None) -> str:
    """读取用户上次选择的主题；缺失或非法时回落到日间。"""
    if path is None:
        return DEFAULT_THEME
    try:
        value = path.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return DEFAULT_THEME
    return value if value in THEMES else DEFAULT_THEME


class _BootstrapApi:
    """Minimal js_api surface: startup info + theme persistence + restart."""

    def __init__(self, holder: dict[str, Any], theme_path: Path | None = None):
        self._holder = holder
        self._theme_path = theme_path
        self._window: Any = None

    def attach_window(self, window: Any) -> None:
        self._window = window

    def bootstrap(self) -> dict[str, Any]:
        return {
            "ready": bool(self._holder.get("ready")),
            "wsUrl": self._holder.get("wsUrl", ""),
            "token": self._holder.get("token", ""),
            "error": self._holder.get("error", ""),
        }

    def restart_app(self) -> bool:
        """保存配置后的重启入口：停后端 → 关窗 → worker 拉起新进程。

        先让后端收敛（端口/文件句柄释放），新进程由 worker 在事件循环关闭
        后拉起；窗口必须销毁，否则 GUI 循环不会退出。关窗失败时回退为
        False，让前端提示用户手动重启，而不是留下一个断线的窗口。
        """
        self._holder["restart"] = True
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:  # noqa: BLE001 - 关窗失败要回退，不能卡在半途
                logger.warning("failed to destroy window on restart", exc_info=True)
                self._holder["restart"] = False
                return False
        loop = self._holder.get("loop")
        stop = self._holder.get("stop")
        if loop is not None and stop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(stop.set)
        else:
            self._holder["closed"] = True
        return True

    def set_theme(self, theme: str) -> bool:
        """持久化主题选择。file:// 下 localStorage 不可用，所以写文件。"""
        value = str(theme or "").strip().lower()
        if value not in THEMES:
            return False
        if self._theme_path is None:
            return True
        try:
            self._theme_path.parent.mkdir(parents=True, exist_ok=True)
            self._theme_path.write_text(value + "\n", encoding="utf-8")
        except OSError:
            logger.warning("failed to persist web theme", exc_info=True)
            return False
        return True


def _relaunch() -> bool:
    """按当前启动方式拉起新进程（配置保存后的重启）。

    - ``python -m zero``：argv[0] 是 __main__.py，按模块方式重启；
    - 控制台脚本 ``zero``：argv[0] 是脚本路径，用当前解释器执行它；
    - 冻结打包：argv[0] 即可执行文件。
    """
    args = list(sys.argv[1:])
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, *args]
    else:
        entry = sys.argv[0] if sys.argv else ""
        if not entry or entry.endswith("__main__.py"):
            cmd = [sys.executable, "-m", "zero", *args]
        else:
            cmd = [sys.executable, entry, *args]
    try:
        # 后端已退出（端口/句柄已释放），这里拉起的新进程可以直接绑定
        subprocess.Popen(cmd, close_fds=True, start_new_session=True)
    except OSError:
        logger.exception("restart failed: %s", cmd)
        return False
    logger.info("restarted: %s", cmd)
    return True


def run_desktop(
        backend: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        width: int,
        height: int,
        title: str = "zero",
        theme_path: Path | None = None,
) -> None:
    """Open the desktop window and run ``backend(holder)`` in a worker thread."""
    try:
        import webview
    except ImportError as exc:
        raise SystemExit(
            "web 通道需要 pywebview，请先安装可选依赖：\n"
            "  uv pip install -e \".[web]\"") from exc

    holder: dict[str, Any] = {}
    api = _BootstrapApi(holder, theme_path)
    theme = _read_theme(theme_path)
    # 在首屏渲染前注入主题属性，避免先白后黑 / 先黑后白的抖动
    html = STATIC_PATH.read_text(encoding="utf-8").replace(
        '<html lang="zh-CN">',
        f'<html lang="zh-CN" data-theme="{theme}">',
        1,
    )

    window = webview.create_window(
        title,
        html=html,
        js_api=api,
        width=width,
        height=height,
        min_size=(760, 520),
        text_select=True,
    )
    api.attach_window(window)

    def on_closed():
        loop = holder.get("loop")
        stop = holder.get("stop")
        if loop is not None and stop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(stop.set)
        else:
            holder["closed"] = True

    window.events.closed += on_closed

    def worker():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        holder["loop"] = loop
        try:
            loop.run_until_complete(backend(holder))
        except Exception as exc:  # noqa: BLE001 - surface to log + window
            logger.exception("web backend crashed")
            holder["error"] = f"后端异常：{exc}"
        finally:
            holder["loop"] = None
            try:
                # 关闭前先收尾异步生成器，避免残留资源告警
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()
                # 配置保存后的自动重启：后端已收敛，这里拉起新进程
                if holder.get("restart") and not _relaunch():
                    holder["error"] = "重启失败，请手动关闭并重新启动 zero"

    webview.start(func=worker, private_mode=False)
