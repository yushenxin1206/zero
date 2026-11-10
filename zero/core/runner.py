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
import asyncio
from typing import Any
from uuid import uuid4

from zero.config import Config, TOOL_RESULT_MAX_CHARS
from zero.core.cancellation import TurnCancelled
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
from zero.paths import Paths
from zero.providers.base import LLMPayload
from zero.providers.openai import OpenAI
from zero.session.store import SessionStore
from zero.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class AgentRunner:

    def __init__(
            self,
            config: Config,
            running_context: Context,
            task: str = "default",
            tool_registry: ToolRegistry | None = None,
            *,
            hooks: Hooks,
            model_override: str | None = None,
            stop_event: asyncio.Event | None = None,
    ):
        # 运行时配置在构造时按任务从根配置解析：主对话用 task（默认
        # default），上下文压缩用 compact——两者可绑定不同模型，各走
        # 独立 provider client（provider 已是结算好的 ProviderConfig 对象）
        self.runtime_config: AgentRuntimeConfig = config.resolve_runtime(
            task, model_override=model_override)
        self.provider = OpenAI(self.runtime_config.llm_runtime.provider)
        compact_config = config.resolve_runtime("compact")
        self.context = running_context
        self.tool_registry = tool_registry
        # 必填：静默兜底成 no-op 链会让护栏在不经意间失效（fail-open 且无提示）
        self.hooks = hooks
        # 协作式取消：None（子 agent 单次模式等）则永不触发
        self._stop_event = stop_event
        self.compactor = Compactor(
            provider=OpenAI(compact_config.llm_runtime.provider),
            session_runtime=self.runtime_config,
            compact_runtime=compact_config,
            compact_config=config.compact,
            session_store=running_context.session_store,
        )

    def _check_cancelled(self) -> None:
        """安全点检查：用户已请求终止则抛 TurnCancelled。"""
        if self._stop_event is not None and self._stop_event.is_set():
            raise TurnCancelled()

    def _hook_context(self) -> HookContext:
        """构建 hook 透传上下文（复用 tool_context 引用，不做深拷贝）。"""
        tc = self.context.tool_context
        return HookContext(
            channel=self.context.channel,
            session_id=self.context.session_id,
            turn_id=tc.turn_id,
            tool_context=tc,
            stop_event=self._stop_event,
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
        except TurnCancelled:
            # 用户终止：原样放行给 AgentLoop 收尾，不转成 error 事件
            raise
        except Exception as e:  # noqa: BLE001 - report failure to the channel
            logger.exception("Agent runner failed")
            yield StreamEvent(kind="error", content=str(e))

    async def _run_loop(self):
        for _ in range(self.runtime_config.max_iterations):
            self._check_cancelled()
            content_parts: list[str] = []
            tool_call_acc: dict[int, dict] = {}
            usage: TokenUsage | None = None
            finish_reason: str | None = None

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
            # stop_event 传入 provider：思考期长时间无 chunk 时，流内
            # chunk 边界检查到不了，由 provider 在读取处与终止信号竞争
            stream = self.provider.stream_chunks(
                payload, stop_event=self._stop_event)
            try:
                async for kind, data in stream:
                    # 流内安全点：reasoner 思考期只流 reasoning chunk（这里
                    # 全部跳过），若只在流结束后检查，终止请求要等整条流
                    # 走完才生效——reasoner 的思考阶段可能很长，逐 chunk
                    # 检查保证用户终止能及时停下
                    self._check_cancelled()
                    if kind == "content":
                        content_parts.append(data)
                        yield StreamEvent(kind="delta", content=data)
                    elif kind == "reasoning":
                        # 透传思考过程供 channel 实时展示（CLI 用 dim 面板）。
                        # 刻意不进 content_parts：reasoning 不参与最终回复、
                        # 不落盘，仅作展示；终止安全点仍由上面的
                        # _check_cancelled 保证（reasoner 思考期也逐 chunk 检查）
                        yield StreamEvent(kind="reasoning", content=data)
                    elif kind == "tool_calls":
                        self._accumulate_tool_call(tool_call_acc, data)
                    elif kind == "usage":
                        usage = TokenUsage.from_openai(data)
                    elif kind == "finish":
                        # provider 透出的收尾原因（"stop"/"length"/...），
                        # 用于识别 max_tokens 截断
                        finish_reason = data
            finally:
                # 提前退出（取消/异常）时关闭生成器释放底层 HTTP 连接，
                # 避免半途放弃的流占用连接；正常结束的 aclose 是空操作
                await stream.aclose()

            content = "".join(content_parts)
            tool_calls_raw = [tool_call_acc[i] for i in sorted(tool_call_acc)]

            # LLM 一轮流式结束后的安全点：期间收到终止请求则停下，
            # 已产出的 delta 片段已送达 channel，由 AgentLoop 收尾
            self._check_cancelled()

            # No tool calls: final text reply
            if not tool_calls_raw:
                # finish_reason=length：模型被 max_tokens 截断。reasoning 与
                # 正文共享 completion 预算，思考吃满时正文会被截断为空——
                # 此前被静默当正常终稿放行（现象：空回复无任何提示）。改为走
                # error 通道明确报错，不产出残缺正文（子 agent 侧 run_subagent
                # 收到 error 即抛 RuntimeError，不会被当成有效结果回投）。
                if finish_reason == "length":
                    yield StreamEvent(
                        kind="error",
                        content=(
                            "模型输出达到 max_tokens 被截断"
                            "（finish_reason=length）：reasoning 与正文共享 "
                            "completion 预算，正文可能不完整或为空"),
                    )
                    return
                if not content.strip():
                    # 无 tool_calls 且正文为空：模型未产出任何可交付内容
                    yield StreamEvent(
                        kind="error",
                        content=f"模型返回空正文（finish_reason={finish_reason}）",
                    )
                    return
                yield StreamEvent(
                    kind="done",
                    content=content,
                    token_usage=usage,
                )
                return

            # Parse tool calls and persist assistant message with tool_calls
            tool_calls = [self._parse_tool_call(tc) for tc in tool_calls_raw]
            # 持久化前的安全点：此时取消直接停下、不留孤儿 assistant(tool_calls)
            self._check_cancelled()
            await self._persist_assistant_with_tools(content, tool_calls, usage)

            # Execute each tool and persist the result
            executed: set[str] = set()
            try:
                for tc in tool_calls:
                    # 工具执行前的安全点：工具是长协程，边界检查不打断中途
                    self._check_cancelled()
                    yield StreamEvent(
                        kind="tool_call",
                        content=f"{tc.function.name}({tc.function.arguments})",
                    )
                    await self._execute_and_persist(tc)
                    executed.add(tc.id)
            except (TurnCancelled, asyncio.CancelledError, GeneratorExit) as e:
                # 已落盘 assistant(tool_calls) 但部分工具未执行：补齐占位
                # tool 结果，保证历史里每个 tool_call_id 都有对应 tool 消息，
                # 否则下一轮加载该残缺序列时 API 报 400。
                # 必须同时捕获 CancelledError/GeneratorExit：应用退出（如
                # Windows Ctrl+C 触发 asyncio Runner 取消所有 task）时，
                # 取消会直穿工具循环——except TurnCancelled 匹配不上它，
                # 曾经因此留下孤儿 tool_calls（历史污染 → 后续 400）。
                # 补齐落盘必须同步执行：被取消的 task 里再 await 会立即
                # 再抛 CancelledError，占位永远写不进去。
                # 占位文案按来源区分：谎称「用户取消」会误导 LLM 的后续
                # 行为，也会误导事后排查（系统取消 ≠ 用户意图）。
                reason = ("turn cancelled by user" if isinstance(e, TurnCancelled)
                          else "aborted: runner cancelled (application shutdown)")
                for tc in tool_calls:
                    if tc.id not in executed:
                        self._persist_tool_result(
                            tc,
                            json.dumps({"error": reason}, ensure_ascii=False),
                        )
                raise

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
        await self.context.arecord(msg_out)

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
                    # 审批附注并入工具结果：随 role=tool 消息进入会话历史，
                    # 让 LLM 知道本次操作经过了什么审批（放行/授予档位）
                    if decision is not None and decision.proceed and decision.note:
                        try:
                            payload = json.loads(content)
                        except ValueError:
                            payload = None
                        if isinstance(payload, dict):
                            payload["permission"] = decision.note
                            content = json.dumps(payload, ensure_ascii=False)
                        else:
                            content = f"{content}\n\n[permission] {decision.note}"
                    # 截断是核心安全不变量，永远在 hook 之后执行
                    if len(content) > TOOL_RESULT_MAX_CHARS:
                        content = content[:TOOL_RESULT_MAX_CHARS] + "...truncated"
                except TurnCancelled:
                    # 审批 hook 被打断（用户终止）：终止信号必须原样上抛，
                    # 不能被 when-tool 错误吞掉，否则工具循环继续执行、
                    # 终止不生效。由外层 _run_loop 的 except TurnCancelled
                    # 统一补齐占位 tool 结果。
                    raise
                except Exception as e:
                    logger.exception("Tool execution failed: %s", tc.function.name)
                    content = json.dumps({"error": str(e)}, ensure_ascii=False)

        # 工具结果统一经 _persist_tool_result_async 落盘，占位结果（取消补齐）
        # 走同步版本：取消语境里 await 会立刻再抛，不能丢占位。
        await self._persist_tool_result_async(tc, content)

    def _tool_result_message(self, tc: ToolCall, content: str) -> MessageTool:
        return MessageTool(
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

    async def _persist_tool_result_async(self, tc: ToolCall, content: str) -> None:
        """正常异步路径：工具结果写入线程池，不阻塞事件循环。"""
        await self.context.arecord(self._tool_result_message(tc, content))

    def _persist_tool_result(self, tc: ToolCall, content: str) -> None:
        """仅取消/退出语境使用：task 已取消，不能再 await。"""
        self.context.record(self._tool_result_message(tc, content))


async def run_subagent(
        prompt: str,
        *,
        config: Config,
        hooks: Hooks,
        session_store: SessionStore,
        paths: Paths,
        task: str = "default",
        tool_registry: ToolRegistry | None = None,
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

    内存上下文无会话历史可压（compressible=False），故按该任务运行时的
    窗口结算一个 payload 预算传给 SubAgentContext，由其在超预算时截断旧
    工具结果兜底（否则超窗会直接表现为 API 报错 → RuntimeError）。
    """
    runtime = config.resolve_runtime(task)
    window = runtime.llm_runtime.context_window_tokens
    token_budget = int(min(
        window * config.compact.trigger_ratio,
        window - runtime.llm_call_options.max_tokens
        - runtime.reasoning_token_headroom,
    ))
    context = SubAgentContext(
        prompt,
        session_store=session_store,
        paths=paths,
        tool_registry=tool_registry,
        tool_context=tool_context,
        system_prompt=system_prompt,
        token_budget=token_budget,
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

