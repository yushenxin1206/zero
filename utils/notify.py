"""
Desktop notification helpers (dependency-free).

Windows 上通过 PowerShell 的 NotifyIcon 气球通知实现（Win10+ 会以
toast 通知的形式呈现），fire-and-forget：后台启动一个短生命周期 PowerShell
进程展示通知后自行退出。非 Windows 平台直接返回 False。
"""
import base64
import logging
import subprocess
import sys

logger = logging.getLogger(__name__)

_BALLOON_PS = """
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::Information
$n.Visible = $true
$n.ShowBalloonTip(4000, {title}, {message}, [System.Windows.Forms.ToolTipIcon]::Info)
Start-Sleep -Seconds 5
$n.Dispose()
"""


def _ps_quote(s: str) -> str:
    """Quote a string as a PowerShell single-quoted literal."""
    return "'" + s.replace("'", "''") + "'"


def toast(title: str, message: str) -> bool:
    """Show a Windows balloon/toast notification. Returns False on
    non-Windows platforms or when spawning PowerShell fails."""
    if sys.platform != "win32":
        return False
    try:
        # 通知正文压成单行，避免气球里出现不可控的换行
        message = " ".join((message or "").split())[:120]
        script = _BALLOON_PS.format(
            title=_ps_quote(title), message=_ps_quote(message))
        # -EncodedCommand 要求 UTF-16LE base64，绕开命令行引号转义问题
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-EncodedCommand", encoded],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        return True
    except Exception:
        logger.exception("toast notification failed")
        return False
