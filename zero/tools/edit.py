"""
Tool for editing file content via find-and-replace
"""
import os
import difflib
from typing import Any

from zero.tools.base import Tool
from zero.utils.file import require_absolute_path


class Edit(Tool):

    def __init__(self):
        pass

    @property
    def name(self):
        return "edit"

    @property
    def description(self):
        return (
            "Edit a file by finding an exact string and replacing it with "
            "a new string. The old_string must match exactly and should "
            "appear only once in the file (use replace_all for multiple "
            "occurrences). Returns a diff of the changes made."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path of the file to edit.",
                },
                "old_string": {
                    "type": "string",
                    "description": "The exact text to find in the file.",
                },
                "new_string": {
                    "type": "string",
                    "description": "The replacement text.",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": (
                        "If true, replaces all occurrences of old_string. "
                        "If false (default), requires old_string to appear "
                        "exactly once."
                    ),
                },
            },
            "required": ["file_path", "old_string", "new_string"],
        }

    async def run(self, context=None, **kwargs):
        old_string = kwargs["old_string"]
        new_string = kwargs["new_string"]
        replace_all = kwargs.get("replace_all", False)

        try:
            file_path = str(require_absolute_path(kwargs["file_path"]))
        except ValueError as exc:
            return {
                "success": False,
                "error": str(exc),
                "file_path": str(kwargs.get("file_path") or ""),
            }

        if not os.path.exists(file_path):
            return {
                "success": False,
                "error": f"File not found: {file_path}",
                "file_path": os.path.abspath(file_path),
            }

        if not os.path.isfile(file_path):
            return {
                "success": False,
                "error": f"Path is not a file: {file_path}",
                "file_path": os.path.abspath(file_path),
            }

        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                original_content = f.read()

            if old_string not in original_content:
                return {
                    "success": False,
                    "error": (
                        "old_string not found in file. "
                        "Check for exact whitespace/line ending matches."
                    ),
                    "file_path": os.path.abspath(file_path),
                }

            occurrences = original_content.count(old_string)

            if not replace_all and occurrences > 1:
                return {
                    "success": False,
                    "error": (
                        f"old_string appears {occurrences} times in the file. "
                        f"Set replace_all to True to replace all, or make "
                        f"the match more specific."
                    ),
                    "file_path": os.path.abspath(file_path),
                    "occurrences": occurrences,
                }

            if replace_all:
                new_content = original_content.replace(old_string, new_string)
                replaced_count = occurrences
            else:
                new_content = original_content.replace(old_string, new_string, 1)
                replaced_count = 1

            diff_lines = list(difflib.unified_diff(
                original_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile="original",
                tofile="modified",
            ))
            diff_text = "".join(diff_lines)

            with open(file_path, "w", encoding="utf-8") as f:
                f.write(new_content)

            return {
                "success": True,
                "message": (
                    f"File edited successfully. "
                    f"Replaced {replaced_count} occurrence(s)."
                ),
                "file_path": os.path.abspath(file_path),
                "replacements_made": replaced_count,
                "diff": diff_text if diff_text else "(no changes in content)",
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
                "error": f"Failed to edit file: {e}",
                "file_path": os.path.abspath(file_path),
            }


