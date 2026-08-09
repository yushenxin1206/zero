"""
Agent loop
"""
from pathlib import Path

from bus.queue import MessageBus


class Loop:

    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    def __init__(
            self,
            msg_bus: MessageBus = None,
            workspace: Path = None,
            provider: str = None,
            model: str = None,
            max_tokens: int = 8192,
            context_window_tokens: int = 65536,
            temperature: float | int = 0.1,
            max_loops: int = 50
    ):
        self.msg_bus = msg_bus
        self.workspace = workspace
        self.provider = provider
        self.model = model
        self.max_tokens = max_tokens
        self.context_window_tokens = context_window_tokens
        self.temperature = temperature
        self.max_loops = max_loops


    def run(self):
        pass

