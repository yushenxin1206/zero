"""
1. Receives messages from the bus
2. Builds context with history, memory, skills
3. Calls the agent runner
4. Sends responses back
"""
import asyncio
import logging
import time

from dataclasses import dataclass, field

from zero.bus.queue import MessageBus
from zero.config import Config
from zero.core.cancellation import TurnCancelled
from zero.core.context import Context
from zero.core.hooks import Hooks
from zero.core.runner import AgentRunner
from zero.models import (
    MessageBody, MessageIn, MessageOut, StreamEvent, TokenUsage, ToolContext)
from zero.data_paths import DataPaths
from zero.session.store import SessionStore
from zero.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# 摘要里错误原因的最大长度：够看清是什么错，又不至于把一行日志撕成多行
# （换行会被压成空格，见 _format_turn）。
_ERROR_MAX_CHARS = 200


@dataclass
class TurnStat:
    """一轮的观测事实，供轮次结束时落一行摘要。

    只装 agent loop 自己看得到的东西：模型与 token 用量来自 runner 的事件，
    工具次数来自 tool_call 事件。刻意不含正文与工具参数。
    """
    turn_id: str
    channel: str
    session_id: str
    model: str = "-"
    tools: int = 0
    tokens: TokenUsage | None = None
    status: str = "completed"
    error: str = ""
    started: float = field(default_factory=time.monotonic)


def _format_turn(stat: TurnStat) -> str:
    """单行 key=value 摘要：一条轮次一行，可 grep ``turn status=error``。"""
    tokens = (
        "-" if stat.tokens is None else
        f"{stat.tokens.prompt_tokens}/{stat.tokens.completion_tokens}/"
        f"{stat.tokens.total_tokens}")
    parts = [
        f"turn status={stat.status}",
        f"turn_id={stat.turn_id}",
        f"channel={stat.channel}",
        f"session={stat.channel}:{stat.session_id}",
        f"model={stat.model}",
        f"ms={int((time.monotonic() - stat.started) * 1000)}",
        f"tools={stat.tools}",
        f"tokens={tokens}",
    ]
    if stat.error:
        # 压掉换行/连续空白再截断：异常信息常带多行堆栈或 SDK 的换行包装
        reason = " ".join(stat.error.split())
        parts.append(f"error={reason[:_ERROR_MAX_CHARS]}")
    return " ".join(parts)


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
            session_store: SessionStore,
            data_paths: DataPaths,
            sandbox_available: bool,
            tool_registry: ToolRegistry | None = None,
            *,
            hooks: Hooks,
    ):
        self.msg_bus = msg_bus
        self.config = config
        # 会话存储、数据根与沙箱能力事实由组合根创建并注入：本类不读进程级路径
        self.session_store = session_store
        self.data_paths = data_paths
        self.sandbox_available = sandbox_available
        self.tool_registry = tool_registry
        # 必填：静默兜底成 no-op 链会让护栏在不经意间失效（fail-open 且无提示）
        self.hooks = hooks
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
                # 活跃 turn 先注册再落盘：慢写入/慢历史加载期间到达的停止
                # 信号也能精确命中本轮，而不是被当成「无活跃轮」丢弃
                stop_event = asyncio.Event()
                self._current = (
                    msg_in.channel, msg_in.session_id, msg_in.id)
                self._current_stop_event = stop_event
                try:
                    await self._handle_turn(msg_in, stop_event)
                finally:
                    self._current = None
                    self._current_stop_event = None
        finally:
            stop_task.cancel()

    async def _handle_turn(self, msg_in: MessageIn, stop_event: asyncio.Event):
        """处理一轮并落一行摘要。

        摘要只在 finally 里输出一次，正常 / 取消 / 异常三个出口共用，
        所以异常路径也一定留痕、且不会重复。
        """
        stat = TurnStat(
            turn_id=msg_in.id, channel=msg_in.channel,
            session_id=msg_in.session_id)
        try:
            await self._run_turn(msg_in, stop_event, stat)
        except asyncio.CancelledError:
            stat.status = "cancelled"
            raise
        except BaseException as e:  # noqa: BLE001 - 先留痕再原样抛出
            stat.status = "error"
            stat.error = f"{type(e).__name__}: {e}"
            raise
        finally:
            logger.info(_format_turn(stat))

    async def _run_turn(
            self, msg_in: MessageIn, stop_event: asyncio.Event,
            stat: TurnStat):
        """一轮的实际流程：落盘用户消息、准备上下文、跑 runner、闭合边界。

        所有阻塞文件 IO 都经 asyncio.to_thread 移出事件循环；取消分支
        直接 return（本方法是独立协程，不再位于 while 循环内）。
        """
        # 轮次事务边界：begin 必须先于该轮任何消息落盘。CLI/飞书/Web
        # 共用同一个 AgentLoop，因此这里是最内层且唯一建立边界的地方。
        await asyncio.to_thread(
            self.session_store.begin_turn,
            msg_in.channel, msg_in.session_id, msg_in.id)
        await asyncio.to_thread(
            self.session_store.append, msg_in)
        # turn 边界统一刷新活跃时间戳：msg_in 紧跟出队、无前置分支，
        # 即使本轮中途失败也已标记活跃（turn 内其它 append 不再触索引）
        await asyncio.to_thread(
            self.session_store.touch, msg_in.channel, msg_in.session_id)
        # 首条用户消息自动生成会话标题（已有标题则不覆盖）
        await asyncio.to_thread(
            self.session_store.auto_title, msg_in.channel,
            msg_in.session_id, msg_in.body.content or "")

        # 慢写入期间到达的停止信号：写完当前这一条就收尾，不再继续读历史、
        # 起 runner。线程里的写盘无法硬中断，但最多只多等这一次写入。
        if stop_event.is_set():
            stat.status = "cancelled"
            await self._finish_cancelled(msg_in)
            return

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
            session_store=self.session_store,
            data_paths=self.data_paths,
            sandbox_available=self.sandbox_available,
            tool_registry=self.tool_registry,
            tool_context=tool_context,
        )

        # 历史/compact/提示词/会话目录一次性在线程池读取完成，
        # 后续 runner 迭代只访问内存快照
        await context.aprepare()
        # 历史加载期间也可能收到停止信号：同理，不再起 runner
        if stop_event.is_set():
            stat.status = "cancelled"
            await self._finish_cancelled(msg_in)
            return
        model_override = await asyncio.to_thread(
            self.session_store.get_model,
            msg_in.channel, msg_in.session_id) or None
        runner = AgentRunner(
            config=self.config,
            running_context=context,
            tool_registry=self.tool_registry,
            hooks=self.hooks,
            # 会话级模型绑定：该会话若被 /model 切换到特定模型，
            # 则本轮用绑定模型解析运行时；空串（未绑定）跟随全局
            # default
            model_override=model_override,
            stop_event=stop_event,
        )
        stat.model = runner.runtime_config.llm_call_options.model

        # runner.run_stream() yields incremental events while
        # executing; stamp routing fields and forward them
        reply_text = ""
        final_usage: TokenUsage | None = None
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
                    final_usage = event.token_usage
                elif event.kind == "tool_call":
                    stat.tools += 1
                elif event.kind == "error":
                    # 记录错误：LLM/工具失败（如孤儿 tool_call_id
                    # 触发 400）。不中断消费，但终止后不落盘空回复。
                    saw_error = event.content
        except TurnCancelled:
            # 用户终止：部分回复已通过 delta 送达 channel。
            # 补发一个带 cancelled 标记的收尾流事件 + 最终
            # MessageOut；不落盘——部分回复不进会话历史
            stat.status = "cancelled"
            await self._finish_cancelled(msg_in, reply_text)
            return

        stat.tokens = final_usage
        failed = saw_error is not None
        if failed:
            stat.status = "error"
            stat.error = saw_error

        msg_out = MessageOut(
            session_id=msg_in.session_id,
            channel=msg_in.channel,
            body=MessageBody(
                # 失败轮只发一条错误通知，不携带残缺正文
                content=f"[error] {saw_error}" if failed else reply_text,
                token_usage=final_usage,
            ),
            origin=msg_in.origin,
            # turn_id 让 channel 端能把最终消息精确匹配到触发它的
            # 那一轮，避免定时任务结果与用户回复在 FIFO 队列里错位
            turn_id=msg_in.id,
            channel_session_id=msg_in.channel_session_id,
        )
        if not failed:
            # 失败轮不落盘为 assistant 正文：错误是系统事实，不是模型说过的
            # 话。落盘会让下一轮模型把系统提示当成自己的历史发言，也会让
            # 子 agent 把超限/截断误当有效结果。重载后该轮只保留用户消息。
            await asyncio.to_thread(
                self.session_store.append, msg_out)
        # 仍投递 MessageOut：只渲染终稿的通道（飞书）靠它看到失败原因；
        # CLI/Web 已在 error 流事件上展示过，按 turn 去重不会重复渲染。
        await self.msg_bus.out_queue.enqueue(msg_out)
        # 本轮最后一条消息之后闭合事务；缺这一行的轮次就是
        # 崩溃残留，加载时整轮丢弃而不是补占位。
        await asyncio.to_thread(
            self.session_store.end_turn,
            msg_in.channel, msg_in.session_id, msg_in.id,
            status="error" if failed else "ok")

    async def _finish_cancelled(self, msg_in: MessageIn, partial: str = ""):
        """取消收尾：广播 cancelled done 事件、回最终 MessageOut、闭合轮次。

        部分回复不落盘；轮次边界仍必须闭合，否则加载时会被当成崩溃残留。
        """
        final_event = StreamEvent(
            kind="done",
            content=partial,
            cancelled=True,
            channel=msg_in.channel,
            session_id=msg_in.session_id,
            turn_id=msg_in.id,
            origin=msg_in.origin,
        )
        await self.msg_bus.stream_queue.enqueue(final_event)
        msg_out = MessageOut(
            session_id=msg_in.session_id,
            channel=msg_in.channel,
            body=MessageBody(content=partial),
            origin=msg_in.origin,
            turn_id=msg_in.id,
            channel_session_id=msg_in.channel_session_id,
        )
        await self.msg_bus.out_queue.enqueue(msg_out)
        await asyncio.to_thread(
            self.session_store.end_turn,
            msg_in.channel, msg_in.session_id, msg_in.id,
            status="cancelled")

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
