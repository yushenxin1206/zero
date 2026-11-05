"""
Tool for command execution
"""
import asyncio
import os
import shlex
from typing import Any

from zero.permission import envelope, seatbelt
from zero.tools.base import Tool

# 内核沙箱拒绝的 stderr 特征（macOS：Operation not permitted）
_DENIED_MARKERS = ("Operation not permitted", "sandbox-exec:")

_DENIED_HINT = (
    "该操作被内核沙箱拒绝。若确实需要写包络外路径，请带 "
    "write_paths=[\"/绝对路径\"] 重试以触发扩权审批；凭证与系统路径不可授予，"
    "请改用包络内数据。不要尝试绕过。"
)

_NETWORK_HINT = (
    "本来源无人值守（scheduler / subagent / 非交互渠道），shell 默认断网。"
    "若命令确实需要联网，请让用户在有交互的 CLI/Web 会话中执行；"
    "无人值守来源不能申请网络扩权。"
)


class Shell(Tool):

    def __init__(self):
        pass

    @property
    def name(self):
        return "shell"

    @property
    def description(self):
        return "Use it to execute shell command, return its output."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                },
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory for the command",
                },
                "write_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Absolute paths outside the workspace this command needs "
                        "to write. Declaring them asks for an expansion approval "
                        "upfront instead of failing first. Omit when unnecessary."
                    ),
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        "Timeout in seconds. Increase for long-running commands "
                        "like compilation or installation (default 60, max 600)."
                    ),
                    "minimum": 1,
                    "maximum": 600,
                },
            },
            "required": ["command"],
        }

    async def run(self, context=None, **kwargs):
        command = kwargs["command"]
        working_dir = kwargs.get("working_dir")
        timeout = kwargs.get("timeout", 60)

        if working_dir:
            working_dir = os.path.expanduser(working_dir)

        is_windows = os.name == "nt"

        # 内核沙箱：脱离白名单命中或本机不可用时退化为裸执行——两种情况都
        # 已在 permission.guard 里审批过，这里不重复判定。
        profile_path = None
        env = None
        argv: list[str] = []
        if not is_windows:
            argv = shlex.split(command)
            if envelope.available() and not envelope.matches_unsandbox(argv):
                env = envelope.current_envelope(context)
                profile = seatbelt.render_profile(env)
                argv, profile_path = seatbelt.wrap(argv, profile)

        try:
            if is_windows:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=working_dir,
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=working_dir,
                )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )

        except asyncio.TimeoutError:
            return {
                "returncode": -1,
                "stdout": "",
                "stderr": "",
                "error": (
                    f"Command '{command}' timed out after {timeout} seconds"
                ),
            }
        except FileNotFoundError as e:
            return {
                "returncode": -1,
                "stdout": "",
                "stderr": "",
                "error": f"Command not found: {e}",
            }
        except Exception as e:
            return {
                "returncode": -1,
                "stdout": "",
                "stderr": "",
                "error": f"Failed to execute command: {e}",
            }
        finally:
            # profile 存活期 = 进程生命周期；必须在 communicate() 之后删
            if profile_path is not None:
                profile_path.unlink(missing_ok=True)

        result = {
            "returncode": proc.returncode,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
        }

        if proc.returncode != 0:
            result["error"] = (
                f"Command '{command}' failed with exit code {proc.returncode}"
            )
            if any(m in result["stderr"] for m in _DENIED_MARKERS):
                # 内核兜底拦下：把"缺什么"回投给模型，触发 write_paths 申请
                result["sandbox_denied"] = True
                hints = [_DENIED_HINT]
                if env is not None and not env.network:
                    hints.append(_NETWORK_HINT)
                result["needed"] = " ".join(hints)

        return result

