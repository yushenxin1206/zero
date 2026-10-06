"""
1. Receives messages from the bus
2. Builds context with history, memory, skills
3. Calls the agent runner
4. Sends responses back
"""
import asyncio
import logging

from zero.bus.queue import MessageBus
from zero.config import Config
from zero.core.cancellation import TurnCancelled
from zero.core.context import Context
from zero.core.hooks import Hooks
from zero.core.runner import AgentRunner
from zero.models import (
    MessageBody, MessageOut, StreamEvent, TokenUsage, ToolContext)
from zero.session.store import SessionStore
from zero.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class AgentLoop:

    """
    The agent loop is the core processing engine.

    只持有根配置（Config）：运行时配置由 AgentRunner 构造时按任务解析，
    模型/provider 等事实不在本类留痕。
    """

    def __init__(
            self,
            msg_bus: MessageBus,
            config: Config,
            tool_registry: ToolRegistry | None = None,
            hooks: Hooks | None = None,
    ):
        self.msg_bus = msg_bus
        self.config = config
        self.tool_registry = tool_registry
        self.hooks = hooks or Hooks()
        # 当前活跃 turn（(channel, session_id, turn_id)）与其 stop_event；
        # 由 _run_stop_consumer 消费旁路停止信号后置位
        self._current: tuple[str, str, str] | None = None
        self._current_stop_event: asyncio.Event | None = None

    async def run(self):
        # 旁路终止信号消费协程：stop_queue 里的请求实时置位活跃 turn 的
        # stop_event，不等 in_queue 串行轮到下一条（那要等当前 turn 结束）
        stop_task = asyncio.create_task(self._run_stop_consumer())
        try:
            while True:
                msg_in = await self.msg_bus.in_queue.dequeue()
                SessionStore.append(msg_in)
                # turn 边界统一刷新活跃时间戳：msg_in 紧跟出队、无前置分支，
                # 即使本轮中途失败也已标记活跃（turn 内其它 append 不再触索引）
                SessionStore.touch(msg_in.channel, msg_in.session_id)
                # 首条用户消息自动生成会话标题（已有标题则不覆盖）
                SessionStore.auto_title(
                    msg_in.channel, msg_in.session_id, msg_in.body.content or "")

                # 把当前会话上下文注入到所有工具（定时任务工具据此定位回投目标）
                # turn_id 进 ToolContext 顶层：hooks/插件（如 shell_guard 的审批
                # 事件路由）据此把交互事件精确挂到触发它的那一轮
                tool_context = ToolContext(
                    channel=msg_in.channel,
                    session_id=msg_in.session_id,
                    turn_id=msg_in.id,
                    origin=msg_in.origin,
                    channel_message_id=msg_in.channel_message_id,
                    channel_sender_id=msg_in.channel_sender_id,
                    channel_session_id=msg_in.channel_session_id,
                )

                context = Context(
                    session_id=msg_in.session_id,
                    channel=msg_in.channel,
                    tool_registry=self.tool_registry,
                    tool_context=tool_context,
                )

                # 活跃 turn 注册：stop 信号按 (channel, session_id, turn_id)
                # 精确匹配，避免跨渠道/会话误终止
                self._current = (msg_in.channel, msg_in.session_id, msg_in.id)
                stop_event = asyncio.Event()
                self._current_stop_event = stop_event
                try:
                    runner = AgentRunner(
                        config=self.config,
                        running_context=context,
                        tool_registry=self.tool_registry,
                        hooks=self.hooks,
                        # 会话级模型绑定：该会话若被 /model 切换到特定模型，
                        # 则本轮用绑定模型解析运行时；空串（未绑定）跟随全局
                        # default
                        model_override=SessionStore.get_model(
                            msg_in.channel, msg_in.session_id) or None,
                        stop_event=stop_event,
                    )

                    # runner.run_stream() yields incremental events while
                    # executing; stamp routing fields and forward them
                    reply_text = ""
                    final_usage: TokenUsage | None = None
                    cancelled = False
                    saw_error: str | None = None
                    try:
                        async for event in runner.run_stream():
                            event.channel = msg_in.channel
                            event.session_id = msg_in.session_id
                            event.turn_id = msg_in.id
                            event.origin = msg_in.origin
                            await self.msg_bus.stream_queue.enqueue(event)
                            if event.kind == "delta":
                                reply_text += event.content
                            elif event.kind == "done":
                                reply_text = event.content or reply_text
                                usage_data = (event.metadata or {}).get(
                                    "token_usage")
                                if isinstance(usage_data, dict):
                                    final_usage = TokenUsage(**usage_data)
                            elif event.kind == "error":
                                # 记录错误：LLM/工具失败（如孤儿 tool_call_id
                                # 触发 400）。不中断消费，但终止后不落盘空回复。
                                saw_error = event.content
                    except TurnCancelled:
                        # 用户终止：部分回复已通过 delta 送达 channel。
                        # 补发一个带 cancelled 标记的收尾流事件 + 最终
                        # MessageOut；不落盘——部分回复不进会话历史
                        cancelled = True
                        final_event = StreamEvent(
                            kind="done",
                            content=reply_text,
                            metadata={"cancelled": True},
                            channel=msg_in.channel,
                            session_id=msg_in.session_id,
                            turn_id=msg_in.id,
                            origin=msg_in.origin,
                        )
                        await self.msg_bus.stream_queue.enqueue(final_event)
                        msg_out = MessageOut(
                            session_id=msg_in.session_id,
                            channel=msg_in.channel,
                            body=MessageBody(content=reply_text),
                            origin=msg_in.origin,
                            turn_id=msg_in.id,
                            channel_session_id=msg_in.channel_session_id,
                        )
                        await self.msg_bus.out_queue.enqueue(msg_out)
                        logger.info(
                            "Turn %s cancelled by user (partial %d chars)",
                            msg_in.id, len(reply_text))
                        continue

                    content = reply_text

                    # 出错且未产出任何内容时，不落盘空 assistant（否则每次
                    # LLM 400 都会往历史追加一条无意义空消息，反复污染）。
                    if saw_error is not None and not content.strip():
                        logger.warning(
                            "Turn %s failed (%s); skipping empty persist",
                            msg_in.id, saw_error)
                        continue

                    msg_out = MessageOut(
                        session_id=msg_in.session_id,
                        channel=msg_in.channel,
                        body=MessageBody(
                            content=content,
                            token_usage=final_usage,
                        ),
                        origin=msg_in.origin,
                        # turn_id 让 channel 端能把最终消息精确匹配到触发它的
                        # 那一轮，避免定时任务结果与用户回复在 FIFO 队列里错位
                        turn_id=msg_in.id,
                        channel_session_id=msg_in.channel_session_id,
                    )
                    SessionStore.append(msg_out)
                    await self.msg_bus.out_queue.enqueue(msg_out)
                finally:
                    self._current = None
                    self._current_stop_event = None
        finally:
            stop_task.cancel()

    async def _run_stop_consumer(self) -> None:
        """消费 bus.stop_queue 的旁路终止信号。

        单消费者 + 状态匹配（非扇出分发，故不叫 dispatcher）：仅当请求
        精确匹配当前活跃 turn（channel/session_id/turn_id 全等）时置位其
        stop_event；turn 已结束或属于其它会话的请求无害忽略。
        """
        while True:
            req = await self.msg_bus.stop_queue.dequeue()
            if self._current is not None and req.turn_id \
                    and (req.channel, req.session_id, req.turn_id) == self._current \
                    and self._current_stop_event is not None:
                self._current_stop_event.set()
