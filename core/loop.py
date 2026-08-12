"""
1. Receives messages from the bus
2. Builds context with history, memory, skills
3. Calls the agent runner
4. Sends responses back
"""
import json

from core.context import Context
from bus.queue import MessageBus
from core.runner import AgentRunner
from session.store import SessionStore
from datacls import AgentRuntimeConfig, ProviderConfigs, Message, MessageBody, MessageIn, MessageOut


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
            SessionStore.append(msg_in)
            context = Context(msg_in.session_id, msg_in.channel)

            runner = AgentRunner(
                runtime_config=self.runtime_config,
                running_context=context,
                provider_configs=self.provider_configs,
            )

            result = await runner.run()

            # Extract text content from LLM response
            if isinstance(result, dict):
                choices = result.get("choices", [])
                content = choices[0]["message"]["content"] if choices else json.dumps(result)
            else:
                content = str(result)

            msg_out = MessageOut(
                session_id=msg_in.session_id,
                channel=msg_in.channel,
                body=MessageBody(
                    content=content,
                ),
                metadata=msg_in.metadata,
            )
            SessionStore.append(msg_out)
            await self.msg_bus.out_queue.enqueue(msg_out)
