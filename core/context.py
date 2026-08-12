"""
Build context for agent
"""
from typing import Any

from session.store import SessionStore
from tools.registry import ToolRegistry


class Context:

    def __init__(
            self,
            session_id: str,
            channel: str,
            tool_registry: ToolRegistry | None = None,
    ):
        self.session_id = session_id
        self.channel = channel
        self.tool_registry = tool_registry

    @property
    def _prompt(self) -> str:
        return "You are a helpful assistant, your name is TT."

    @property
    def messages(self) -> list[dict[str, Any]]:
        history = SessionStore.load_history(self.session_id, self.channel)
        msgs: list[dict[str, Any]] = [
            {
                "content": self._prompt,
                "role": "system"
            }
        ]
        for m in history:
            if m.sender == "user":
                msgs.append({"role": "user", "content": m.body.content})
            elif m.sender == "assistant":
                msg: dict[str, Any] = {"role": "assistant", "content": m.body.content}
                if m.body.tool_calls:
                    msg["tool_calls"] = [tc.model_dump() for tc in m.body.tool_calls]
                msgs.append(msg)
            elif m.sender == "tool":
                msgs.append({
                    "role": "tool",
                    "tool_call_id": m.body.tool_call_id,
                    "content": m.body.content,
                })
        return msgs

    @property
    def tools(self) -> list[dict[str, Any]] | None:
        if self.tool_registry is None:
            return None
        schemas = self.tool_registry.schemas()
        return schemas if schemas else None
