"""
Build context for agent
"""
from typing import Any

from session.store import SessionStore


class Context:

    def __init__(
            self,
            session_id: str,
            channel: str,
    ):
        self.session_id = session_id
        self.channel = channel

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
            role = "user" if m.sender == "user" else "assistant"
            msgs.append(
                {
                    "content": m.body.content,
                    "role": role,
                }
            )
        return msgs

    @property
    def tools(self) -> list | None:
        return None
