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
        return (
            "You are a helpful assistant, your name is TT.\n"
            "\n"
            "Guidelines for tool usage:\n"
            "- For simple questions (e.g. greetings, identity, general knowledge), respond directly without calling any tools.\n"
            "- Only use tools when the user explicitly requests file operations or command execution.\n"
            "- Do not explore the environment or read files unless the task requires it.\n"
            "- When tools are needed, use the minimum number of calls to complete the task."
        )

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
