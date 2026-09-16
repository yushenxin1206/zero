"""
Base tool class
"""
from abc import ABC, abstractmethod
from typing import Any

from models import ToolContext


class Tool(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        """工具唯一名称，function calling 的 name"""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """工具描述，给大模型看"""
        pass

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        """JSON Schema 的 parameters 字段"""
        pass

    def to_function_schema(self) -> dict[str, Any]:
        """输出完整 function‑calling schema"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters
            }
        }

    @abstractmethod
    async def run(self, context: ToolContext | None = None, **kwargs):
        """工具实际执行逻辑。context 由 runner 统一注入当前会话上下文。"""
        pass
