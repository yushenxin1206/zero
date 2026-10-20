"""
Tool for writing file content
"""
import os
from typing import Any

from zero import sandbox
from zero.tools.base import Tool


class Write(Tool):

    def __init__(self):
        pass

    @property
    def name(self):
        return "write"

    @property
    def description(self):
        return (
            "Write content to a file. Creates the file if it does not exist, "
            "or overwrites/appends to an existing file. Parent directories "
            "are created automatically if they don't exist."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "The path of the file to write.",
                },
                "content": {
                    "type": "string",
                    "description": "The content to write to the file.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["overwrite", "append"],
                    "description": (
                        "Write mode: 'overwrite' to replace existing content, "
                        "'append' to add content at the end. "
                        "Defaults to 'overwrite'."
                    ),
                },
            },
            "required": ["file_path", "content"],
        }

    async def run(self, context=None, **kwargs):
        file_path = kwargs["file_path"]
        content = kwargs["content"]
        mode = kwargs.get("mode", "overwrite")

        file_path = os.path.expanduser(file_path)

        # 硬边界：写方向是加法白名单。阶段 A 为 fail-closed，不弹审批
        verdict = sandbox.check_writable(file_path, context)
        if not verdict.ok:
            return {
                "success": False,
                "error": verdict.reason,
                "needed": verdict.needed,
                "file_path": os.path.abspath(file_path),
            }

        if mode not in ("overwrite", "append"):
            return {
                "success": False,
                "error": f"Invalid mode: {mode}. Use 'overwrite' or 'append'.",
                "file_path": os.path.abspath(file_path),
            }

        try:
            parent_dir = os.path.dirname(file_path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)

            open_mode = "w" if mode == "overwrite" else "a"
            with open(file_path, open_mode, encoding="utf-8") as f:
                f.write(content)

            return {
                "success": True,
                "message": (
                    f"File written successfully "
                    f"({'overwrite' if mode == 'overwrite' else 'append'} mode)."
                ),
                "file_path": os.path.abspath(file_path),
                "bytes_written": len(content.encode("utf-8")),
            }

        except PermissionError:
            return {
                "success": False,
                "error": f"Permission denied: {file_path}",
                "file_path": os.path.abspath(file_path),
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to write file: {e}",
                "file_path": os.path.abspath(file_path),
            }