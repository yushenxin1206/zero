"""
Tool for reading file content
"""
import os
from typing import Any

from tools.base import Tool


class Read(Tool):

    def __init__(self):
        pass

    @property
    def name(self):
        return "read"

    @property
    def description(self):
        return (
            "Read the contents of a file. Supports specifying a line range "
            "to read only a portion of the file. Returns the file content "
            "with line numbers."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "The path of the file to read.",
                },
                "start_line": {
                    "type": "integer",
                    "description": (
                        "Start line number (1-based). If not specified, "
                        "reads from the beginning."
                    ),
                },
                "end_line": {
                    "type": "integer",
                    "description": (
                        "End line number (1-based, inclusive). If not "
                        "specified, reads to the end of the file."
                    ),
                },
            },
            "required": ["file_path"],
        }

    async def run(self, context=None, **kwargs):
        file_path = kwargs["file_path"]
        start_line = kwargs.get("start_line")
        end_line = kwargs.get("end_line")

        file_path = os.path.expanduser(file_path)

        if not os.path.exists(file_path):
            return {
                "success": False,
                "content": "",
                "error": f"File not found: {file_path}",
            }

        if not os.path.isfile(file_path):
            return {
                "success": False,
                "content": "",
                "error": f"Path is not a file: {file_path}",
            }

        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()

            total_lines = len(all_lines)

            if start_line is not None and start_line < 1:
                start_line = 1
            if end_line is not None and end_line > total_lines:
                end_line = total_lines

            if start_line is None:
                start_line = 1
            if end_line is None:
                end_line = total_lines

            selected = all_lines[start_line - 1:end_line]
            numbered = []
            for i, line in enumerate(selected, start=start_line):
                numbered.append(f"{i:>4} | {line}")

            content = "".join(numbered)

            return {
                "success": True,
                "content": content,
                "total_lines": total_lines,
                "read_range": [start_line, end_line],
                "file_path": os.path.abspath(file_path),
            }

        except PermissionError:
            return {
                "success": False,
                "content": "",
                "error": f"Permission denied: {file_path}",
            }
        except Exception as e:
            return {
                "success": False,
                "content": "",
                "error": f"Failed to read file: {e}",
            }