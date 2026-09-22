"""
Tool for command execution
"""
import asyncio
import os
import shlex
from typing import Any

from zero.tools.base import Tool


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
                    *shlex.split(command),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=working_dir,
                )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )

            result = {
                "returncode": proc.returncode,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
            }

            if proc.returncode != 0:
                result["error"] = (
                    f"Command '{command}' failed with exit code {proc.returncode}"
                )

            return result

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

