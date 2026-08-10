"""
Build context for agent
"""


class ContextBuilder:

    def __init__(self):
        pass

    @property
    def _prompt(self) -> str:
        return "You are a helpful assistant, your name is TT."

    @property
    def messages(self) -> list:
        return [
                    {
                        "content": self._prompt,
                        "role": "system"
                    }
                ]

    @property
    def tools(self) -> list | None:
        return None

