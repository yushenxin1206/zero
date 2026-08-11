"""
Agent runner:
1. Receives messages from the bus
2. Builds context with history, memory, skills
3. Invoke agent loop
4. Sends responses back
"""
from core import context
from core.context import Context
from datacls import AgentRuntimeConfig, ProviderConfigs, LLMPayload, LLMConfig
from providers.openai import OpenAI

from config import default_config


class AgentRunner:

    def __init__(
            self,
            runtime_config: AgentRuntimeConfig,
            provider_configs: ProviderConfigs,
            running_context: Context,
    ):
        self.runtime_config = runtime_config
        self.provider = OpenAI(provider_configs.get(runtime_config.provider))
        self.context = running_context

    async def run(self):
        resp = await self.provider.chat_with_llm(
            payload=LLMPayload(
                messages=self.context.messages,
                tools=self.context.tools,
                llm_config=self.runtime_config.llm_config
            )
        )
        return resp
