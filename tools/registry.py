"""
Tool registry for managing available tools.
"""
from typing import Any

from tools.base import Tool


class ToolRegistry:
    """Registry of tool instances, keyed by tool name."""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        """Return OpenAI-compatible function schemas for all tools."""
        return [t.to_function_schema() for t in self._tools.values()]
