"""
1. Receives messages from the bus
2. Builds context with history, memory, skills
3. Calls the agent runner
4. Sends responses back
"""
from core.context import Context
from bus.queue import MessageBus
from core.runner import AgentRunner
from datacls import AgentRuntimeConfig, ProviderConfigs, LLMPayload


class AgentLoop:

    """
    The agent loop is the core processing engine.
    """

    def __init__(
            self,
            msg_bus: MessageBus,
            runtime_config: AgentRuntimeConfig,
            provider_configs: ProviderConfigs,
    ):
        self.msg_bus = msg_bus
        self.runtime_config = runtime_config
        self.provider_configs = provider_configs

    async def run(self):

        while True:
            msg_in = await self.msg_bus.in_queue.dequeue()
            context = Context(msg_in.session_id, msg_in.body.content)

            runner = AgentRunner(
                runtime_config=self.runtime_config,
                running_context=context,
                provider_configs=self.provider_configs,
            )

            return await runner.run()

