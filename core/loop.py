"""
1. Receives messages from the bus
2. Builds context with history, memory, skills
3. Calls the agent runner
4. Sends responses back
"""
from bus.queue import MessageBus
from core.context import Context
from core.runner import AgentRunner
from datacls import AgentRuntimeConfig, MessageBody, MessageOut, ProviderConfigs, ToolContext
from session.store import SessionStore
from tools.registry import ToolRegistry


class AgentLoop:

    """
    The agent loop is the core processing engine.
    """

    def __init__(
            self,
            msg_bus: MessageBus,
            runtime_config: AgentRuntimeConfig,
            provider_configs: ProviderConfigs,
            tool_registry: ToolRegistry | None = None,
    ):
        self.msg_bus = msg_bus
        self.runtime_config = runtime_config
        self.provider_configs = provider_configs
        self.tool_registry = tool_registry

    async def run(self):

        while True:
            msg_in = await self.msg_bus.in_queue.dequeue()
            SessionStore.append(msg_in)

            # 把当前会话上下文注入到所有工具（定时任务工具据此定位回投目标）
            tool_context = ToolContext(
                channel=msg_in.channel,
                session_id=msg_in.session_id,
                message_id=msg_in.metadata.get("message_id", ""),
                sender_id=msg_in.metadata.get("sender_id", ""),
                metadata=msg_in.metadata,
            )

            context = Context(
                session_id=msg_in.session_id,
                channel=msg_in.channel,
                tool_registry=self.tool_registry,
                tool_context=tool_context,
            )

            runner = AgentRunner(
                runtime_config=self.runtime_config,
                running_context=context,
                provider_configs=self.provider_configs,
                tool_registry=self.tool_registry,
            )

            # runner.run() returns final assistant text after all tool calls
            content = await runner.run()

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
