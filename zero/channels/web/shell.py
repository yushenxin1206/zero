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
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

STATIC_PATH = Path(__file__).with_name("static") / "index.html"
APP_ICON_PATH = Path(__file__).with_name("static") / "zero-icon.png"

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
        """保存配置后的重启入口：只停后端，窗口由 worker 在收尾时关闭。

        千万不能在 js_api 线程里同步关窗：那会先结束 GUI 循环，让后端在
        无窗口状态下继续收尾，旧进程既退不掉、端口也没释放，表现为
        「点了重启窗口还在但没响应」。正确顺序由 worker 统一执行：
        后端收敛 → 拉起新进程 → 关窗 → 退出旧进程。
        """
        self._holder["restart"] = True
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

    def pick_folder(self, start: str = "") -> str:
        """系统原生目录选择器；取消或不可用时返回空串。"""
        if self._window is None:
            return ""
        try:
            import webview
        except ImportError:  # pragma: no cover - 运行桌面端必然已安装
            return ""
        directory = start if start and Path(start).is_dir() else ""
        try:
            result = self._window.create_file_dialog(
                webview.FileDialog.FOLDER, directory=directory)
        except Exception:  # noqa: BLE001 - 对话框失败不能拖垮窗口
            logger.warning("folder picker failed", exc_info=True)
            return ""
        if not result:
            return ""
        return str(result[0])


def _relaunch() -> bool:
    """按当前启动方式拉起新进程（配置保存后的重启）。

    - ``python -m zero``：argv[0] 是 __main__.py，按模块方式重启；
    - 控制台脚本 ``zero``：argv[0] 是脚本路径，用当前解释器执行它。
    """
    args = list(sys.argv[1:])
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
                if holder.get("restart"):
                    # 顺序是硬约束：后端收敛 → 拉起新进程 → 关窗 → 退出旧进程。
                    # 关窗排在最后，旧窗口才不会在无后端状态下空转成「假死」。
                    if not _relaunch():
                        logger.error("重启失败：无法拉起新进程，请手动重新启动 zero")
                    try:
                        window.destroy()
                    except Exception:  # noqa: BLE001 - 关窗失败也要退出旧进程
                        logger.warning("关闭旧窗口失败", exc_info=True)
                    # 给主线程一点时间处理关窗，再强制退出，避免
                    # GUI 线程与后台线程互相等待导致进程挂着不退
                    time.sleep(0.4)
                    os._exit(0)

    webview.start(
        func=worker, private_mode=False, icon=str(APP_ICON_PATH))
