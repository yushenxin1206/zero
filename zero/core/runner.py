"""
Agent runner：agent 核心循环，两种调用方式共享同一实现。

1. 会话模式（AgentLoop）：每 turn 由 loop 构造，历史读写 SessionStore，
   中间消息（assistant tool_calls / tool 结果）落会话存储
2. 子 agent 单次模式（run_subagent）：以 SubAgentContext 隔离运行，
   prompt 是唯一输入，中间消息只留内存，不污染任何会话

runner 通过 context.record() / context.compressible 与上下文解耦，
自身不感知消息落盘还是留内存。
"""
import json
import logging
from typing import Any
from uuid import uuid4

from zero.config import Config, TOOL_RESULT_MAX_CHARS
from zero.core.compactor import Compactor
from zero.core.context import Context, SubAgentContext
from zero.core.hooks import HookContext, Hooks
from zero.models import (
    AgentRuntimeConfig,
    MessageBody,
    MessageOut,
    MessageTool,
    StreamEvent,
    TokenUsage,
    ToolCall,
    ToolCallFunction,
    ToolContext,
)
from zero.providers.base import LLMPayload
from zero.providers.openai import OpenAI
from zero.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class AgentRunner:

    def __init__(
            self,
            config: Config,
            running_context: Context,
            task: str = "default",
            tool_registry: ToolRegistry | None = None,
            hooks: Hooks | None = None,
    ):
        # 运行时配置在构造时按任务从根配置解析：主对话用 task（默认
        # default），上下文压缩用 compact——两者可绑定不同模型，各走
        # 独立 provider client（provider 已是结算好的 ProviderConfig 对象）
        self.runtime_config: AgentRuntimeConfig = config.resolve_runtime(task)
        self.provider = OpenAI(self.runtime_config.llm_runtime.provider)
        compact_config = config.resolve_runtime("compact")
        self.context = running_context
        self.tool_registry = tool_registry
        self.hooks = hooks or Hooks()
        self.compactor = Compactor(
            provider=OpenAI(compact_config.llm_runtime.provider),
            session_runtime=self.runtime_config,
            compact_runtime=compact_config,
            compact_config=config.compact,
        )

    def _hook_context(self) -> HookContext:
        """构建 hook 透传上下文（复用 tool_context 引用，不做深拷贝）。"""
        tc = self.context.tool_context
        return HookContext(
            channel=self.context.channel,
            session_id=self.context.session_id,
            turn_id=tc.turn_id,
            tool_context=tc,
        )

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
            usage: TokenUsage | None = None

            # 每次构建 payload 之前检查是否触发上下文压缩（第 0 次迭代即
            # turn 起点；压缩后 context.messages 自动合成 [摘要消息] + 尾部）。
            # 纯内存上下文（SubAgentContext）无会话历史可压，跳过
            if self.context.compressible:
                await self.compactor.maybe_compress(context=self.context)

            payload = LLMPayload(
                messages=self.context.messages,
                tools=self.context.tools,
                llm_call_options=self.runtime_config.llm_call_options.model_copy(),
            )
            async for kind, data in self.provider.stream_chunks(payload):
                if kind == "content":
                    content_parts.append(data)
                    yield StreamEvent(kind="delta", content=data)
                elif kind == "reasoning":
                    # 暂不处理reasoning
                    continue
                elif kind == "tool_calls":
                    self._accumulate_tool_call(tool_call_acc, data)
                elif kind == "usage":
                    usage = TokenUsage.from_openai(data)

            content = "".join(content_parts)
            tool_calls_raw = [tool_call_acc[i] for i in sorted(tool_call_acc)]

            # No tool calls: final text reply
            if not tool_calls_raw:
                yield StreamEvent(
                    kind="done",
                    content=content,
                    metadata=(
                        {"token_usage": usage.model_dump(by_alias=True)}
                        if usage else {}
                    ),
                )
                return

            # Parse tool calls and persist assistant message with tool_calls
            tool_calls = [self._parse_tool_call(tc) for tc in tool_calls_raw]
            await self._persist_assistant_with_tools(content, tool_calls, usage)

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
            usage: TokenUsage | None = None,
    ) -> None:
        """Record assistant message that triggered tool calls.

        经 context.record() 落盘（会话上下文）或留内存（子 agent 上下文）。
        """
        msg_out = MessageOut(
            session_id=self.context.session_id,
            channel=self.context.channel,
            body=MessageBody(
                content=content,
                tool_calls=tool_calls,
                token_usage=usage,
            ),
        )
        self.context.record(msg_out)

    async def _execute_and_persist(self, tc: ToolCall) -> None:
        """Execute a single tool call and record its result
        (SessionStore 或内存，取决于上下文实现)。"""
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
                    # ④ before_tool_call：唯一调用点。否决/替换结果照常走
                    # MessageTool 持久化，LLM 和会话历史都看得到。
                    decision = await self.hooks.before_tool_call(
                        tool.name, kwargs, self._hook_context())
                    if decision is not None and not decision.proceed:
                        content = json.dumps(
                            {"error": decision.reason}, ensure_ascii=False)
                    elif decision is not None and decision.result is not None:
                        content = json.dumps(decision.result, ensure_ascii=False)
                    else:
                        result = await tool.run(
                            context=self.context.tool_context,
                            **kwargs,
                        )
                        content = json.dumps(result, ensure_ascii=False)
                    # 截断是核心安全不变量，永远在 hook 之后执行
                    if len(content) > TOOL_RESULT_MAX_CHARS:
                        content = content[:TOOL_RESULT_MAX_CHARS] + "...truncated"
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
        )
        self.context.record(tool_msg)


async def run_subagent(
        prompt: str,
        *,
        config: Config,
        task: str = "default",
        tool_registry: ToolRegistry | None = None,
        hooks: Hooks | None = None,
        tool_context: ToolContext | None = None,
        system_prompt: str | None = None,
) -> str:
    """以子 agent 身份单次执行任务，返回最终回复文本。

    与 AgentLoop 的常驻会话用法完全隔离：
    - prompt 是唯一输入消息；工具循环的全部中间消息只留在内存
      （SubAgentContext），不读写 SessionStore，不污染任何会话历史
    - config 是根配置，运行时配置按 task 解析（默认 default 主对话口径）
    - tool_registry 由调用方给定（通常是裁剪后的子集）
    - tool_context 建议传父轮的 ToolContext：channel/turn_id 等路由字段
      被继承，hooks（如 shell_guard 审批）才能把交互事件挂回触发轮
    - hooks 透传父级链：子 agent 的工具调用受同一套护栏约束
    - 失败抛 RuntimeError（error 事件的错误文本），不返回残缺回复
    """
    context = SubAgentContext(
        prompt,
        tool_registry=tool_registry,
        tool_context=tool_context,
        system_prompt=system_prompt,
    )
    runner = AgentRunner(
        config=config,
        running_context=context,
        task=task,
        tool_registry=tool_registry,
        hooks=hooks,
    )
    reply = ""
    async for event in runner.run_stream():
        if event.kind == "error":
            raise RuntimeError(event.content)
        if event.kind == "delta":
            reply += event.content
        elif event.kind == "done":
            reply = event.content or reply
    return reply

