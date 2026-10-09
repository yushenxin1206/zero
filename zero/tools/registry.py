"""
Tool registry for managing available tools.
"""
from collections.abc import Iterable
from typing import Any

from zero.tools.base import Tool


class ToolRegistry:
    """Registry of tool instances, keyed by tool name."""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def subset(self, exclude: Iterable[str] = ()) -> "ToolRegistry":
        """返回剔除指定工具名后的新注册表（工具实例共享，不做拷贝）。

        子 agent 工具集裁剪用：从主对话全量工具集中排除 shell / 递归
        subagent 等不应暴露的工具，得到独立注册表。
        """
        excluded = set(exclude)
        reg = ToolRegistry()
        for name, tool in self._tools.items():
            if name not in excluded:
                reg.register(tool)
        return reg

    def schemas(self) -> list[dict[str, Any]]:
        """Return OpenAI-compatible function schemas for all tools."""
        return [t.to_function_schema() for t in self._tools.values()]
