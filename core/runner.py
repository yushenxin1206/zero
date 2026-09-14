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
    StreamEvent,
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
        Execute the agent loop with tool-calling support (non-streaming).

        Returns the final assistant text content after all tool calls
        have been resolved. Intermediate messages (assistant with tool_calls
        and tool results) are persisted to session storage during the loop.
        """
        reply = ""
        async for event in self.run_stream():
            if event.kind == "delta":
                reply += event.content
            elif event.kind == "done":
                reply = event.content or reply
        return reply

    async def run_stream(self):
        """
        Streaming variant of run(): yields StreamEvent while executing.

        - "delta" events carry assistant text chunks as they arrive
        - "tool_call" events are emitted before each tool execution
        - a final "done" event carries the full reply text
        - on failure an "error" event is yielded (then the generator stops)

        channel / session_id are left empty here; the caller (AgentLoop)
        stamps them before publishing to the bus.
        """
        try:
            async for event in self._run_loop():
                yield event
        except Exception as e:  # noqa: BLE001 - report failure to the channel
            logger.exception("Agent runner failed")
            yield StreamEvent(kind="error", content=str(e))

    async def _run_loop(self):
        for _ in range(self.runtime_config.max_iterations):
            content_parts: list[str] = []
            tool_call_acc: dict[int, dict] = {}

            payload = LLMPayload(
                messages=self.context.messages,
                tools=self.context.tools,
                llm_config=self.runtime_config.llm_config.model_copy(),
            )
            async for kind, data in self.provider.stream_chunks(payload):
                if kind == "content":
                    content_parts.append(data)
                    yield StreamEvent(kind="delta", content=data)
                elif kind == "reasoning":
                    logger.info("[reasoning] %s", data)
                elif kind == "tool_calls":
                    self._accumulate_tool_call(tool_call_acc, data)

            content = "".join(content_parts)
            tool_calls_raw = [tool_call_acc[i] for i in sorted(tool_call_acc)]

            # No tool calls: final text reply
            if not tool_calls_raw:
                yield StreamEvent(kind="done", content=content)
                return

            # Parse tool calls and persist assistant message with tool_calls
            tool_calls = [self._parse_tool_call(tc) for tc in tool_calls_raw]
            await self._persist_assistant_with_tools(content, tool_calls)

            # Execute each tool and persist the result
            for tc in tool_calls:
                yield StreamEvent(
                    kind="tool_call",
                    content=f"{tc.function.name}({tc.function.arguments})",
                )
                await self._execute_and_persist(tc)

        logger.warning("Agent runner exceeded max_iterations=%d", self.runtime_config.max_iterations)
        yield StreamEvent(kind="done", content="Agent runner exceeded max_iterations.")

    @staticmethod
    def _accumulate_tool_call(acc: dict[int, dict], raw: dict) -> None:
        """Merge a streamed tool_call fragment into the accumulator by index."""
        idx = raw.get("index", 0)
        slot = acc.setdefault(idx, {
            "id": "",
            "type": "function",
            "function": {"name": "", "arguments": ""},
        })
        if raw.get("id"):
            slot["id"] = raw["id"]
        if raw.get("type"):
            slot["type"] = raw["type"]
        func = raw.get("function") or {}
        if func.get("name"):
            slot["function"]["name"] += func["name"]
        if func.get("arguments"):
            slot["function"]["arguments"] += func["arguments"]

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
                    result = await tool.run(
                        context=self.context.tool_context,
                        **kwargs,
                    )
                    content = json.dumps(result, ensure_ascii=False)
                    if len(content) > 10000:
                        content = content[:10000] + "...truncated"
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

