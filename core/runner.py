"""
Agent runner:
1. Receives messages from the bus
2. Builds context with history, memory, skills
3. Invoke agent loop
4. Sends responses back
"""

from bus.queue import MessageBus
from core.context import ContextBuilder
from core import AgentConfig
from providers.openai import OpenAI


class AgentRunner:

    def __init__(
            self,
            msg_bus: MessageBus,
            config: AgentConfig,
    ):
        self.msg_bus = msg_bus
        self.config = config

    async def run(self):
        """
            1. Receives messages from the bus
            2. Builds context with history, memory, skills
            3. Calls the LLM
            4. Executes tool calls
            5. Sends responses back
        """

        while True:
            msg_in = await self.msg_bus.in_queue.dequeue()

