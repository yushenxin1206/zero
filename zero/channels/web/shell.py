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
    """Minimal js_api surface: startup info + theme persistence."""

    def __init__(self, holder: dict[str, Any], theme_path: Path | None = None):
        self._holder = holder
        self._theme_path = theme_path

    def bootstrap(self) -> dict[str, Any]:
        return {
            "ready": bool(self._holder.get("ready")),
            "wsUrl": self._holder.get("wsUrl", ""),
            "token": self._holder.get("token", ""),
            "error": self._holder.get("error", ""),
        }

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

    webview.start(func=worker, private_mode=False)
