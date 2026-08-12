"""
Agent runner:
1. Receives messages from the bus
2. Builds context with history, memory, skills
3. Invoke agent loop
4. Sends responses back
"""
import json
import logging
from typing import Any
from uuid import uuid4

from core.context import Context
from datacls import (
    AgentRuntimeConfig,
    ProviderConfigs,
    LLMPayload,
    MessageBody,
    MessageOut,
    MessageTool,
    ToolCall,
    ToolCallFunction,
)
from providers.openai import OpenAI
from session.store import SessionStore
from tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class AgentRunner:

    def __init__(
            self,
            runtime_config: AgentRuntimeConfig,
            provider_configs: ProviderConfigs,
            running_context: Context,
            tool_registry: ToolRegistry | None = None,
    ):
        self.runtime_config = runtime_config
        self.provider = OpenAI(provider_configs.get(runtime_config.provider))
        self.context = running_context
        self.tool_registry = tool_registry

    async def run(self) -> str:
        """
        Execute the agent loop with tool-calling support.

        Returns the final assistant text content after all tool calls
        have been resolved. Intermediate messages (assistant with tool_calls
        and tool results) are persisted to session storage during the loop.
        """
        for _ in range(self.runtime_config.max_iterations):
            resp = await self.provider.chat_with_llm(
                payload=LLMPayload(
                    messages=self.context.messages,
                    tools=self.context.tools,
                    llm_config=self.runtime_config.llm_config,
                )
            )

            choice = resp.get("choices", [{}])[0]
            msg = choice.get("message", {})
            tool_calls_raw = msg.get("tool_calls")

            # No tool calls: final text reply
            if not tool_calls_raw:
                return msg.get("content", "") or ""

            # Parse tool calls and persist assistant message with tool_calls
            tool_calls = [self._parse_tool_call(tc) for tc in tool_calls_raw]
            await self._persist_assistant_with_tools(msg.get("content"), tool_calls)

            # Execute each tool and persist the result
            for tc in tool_calls:
                await self._execute_and_persist(tc)

        logger.warning("Agent runner exceeded max_iterations=%d", self.runtime_config.max_iterations)
        return "抱歉，处理过程超过最大迭代次数，请稍后重试。"

    @staticmethod
    def _parse_tool_call(raw: dict[str, Any]) -> ToolCall:
        """Parse a tool_call dict from LLM API response into ToolCall model."""
        func = raw.get("function", {})
        return ToolCall(
            id=raw.get("id", ""),
            type=raw.get("type", "function"),
            function=ToolCallFunction(
                name=func.get("name", ""),
                arguments=func.get("arguments", "{}"),
            ),
        )

    async def _persist_assistant_with_tools(
            self,
            content: str | None,
            tool_calls: list[ToolCall],
    ) -> None:
        """Persist assistant message that triggered tool calls."""
        msg_out = MessageOut(
            session_id=self.context.session_id,
            channel=self.context.channel,
            body=MessageBody(
                content=content,
                tool_calls=tool_calls,
            ),
            metadata={},
        )
        SessionStore.append(msg_out)

    async def _execute_and_persist(self, tc: ToolCall) -> None:
        """Execute a single tool call and persist its result."""
        if self.tool_registry is None:
            content = json.dumps({"error": "no tool registry available"}, ensure_ascii=False)
        else:
            tool = self.tool_registry.get(tc.function.name)
            if tool is None:
                content = json.dumps(
                    {"error": f"tool '{tc.function.name}' not found"},
                    ensure_ascii=False,
                )
            else:
                try:
                    kwargs = json.loads(tc.function.arguments or "{}")
                    result = await tool.run(**kwargs)
                    content = json.dumps(result, ensure_ascii=False)
                except Exception as e:
                    logger.exception("Tool execution failed: %s", tc.function.name)
                    content = json.dumps({"error": str(e)}, ensure_ascii=False)

        tool_msg = MessageTool(
            id=str(uuid4()),
            session_id=self.context.session_id,
            channel=self.context.channel,
            sender="tool",
            recipient="assistant",
            body=MessageBody(
                content=content,
                tool_call_id=tc.id,
            ),
            metadata={},
        )
        SessionStore.append(tool_msg)
