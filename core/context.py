"""
Build context for agent
"""
from bus.queue import MessageBus


class Context:

    def __init__(
            self,
            session_id: str,
            user_input: str
    ):
        self.session_id = session_id
        self.user_input = user_input

    @property
    def _prompt(self) -> str:
        return "You are a helpful assistant, your name is TT."

    @property
    def messages(self) -> list:
        return [
                    {
                        "content": self._prompt,
                        "role": "system"
                    },
                    {
                        "content": self.user_input,
                        "role": "user"
                    }
                ]

    @property
    def tools(self) -> list | None:
        return None

