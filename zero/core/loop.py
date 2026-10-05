"""
1. Receives messages from the bus
2. Builds context with history, memory, skills
3. Calls the agent runner
4. Sends responses back
"""
from zero.bus.queue import MessageBus
from zero.config import Config
from zero.core.context import Context
from zero.core.hooks import Hooks
from zero.core.runner import AgentRunner
from zero.models import MessageBody, MessageOut, TokenUsage, ToolContext
from zero.session.store import SessionStore
from zero.tools.registry import ToolRegistry


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

    async def run(self):

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

            runner = AgentRunner(
                config=self.config,
                running_context=context,
                tool_registry=self.tool_registry,
                hooks=self.hooks,
                # 会话级模型绑定：该会话若被 /model 切换到特定模型，则本轮
                # 用绑定模型解析运行时；空串（未绑定）跟随全局 default
                model_override=SessionStore.get_model(
                    msg_in.channel, msg_in.session_id) or None,
            )

            # runner.run_stream() yields incremental events while executing;
            # stamp routing fields and forward them to subscribed channels
            reply_text = ""
            final_usage: TokenUsage | None = None
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
                    usage_data = (event.metadata or {}).get("token_usage")
                    if isinstance(usage_data, dict):
                        final_usage = TokenUsage(**usage_data)
            content = reply_text

            msg_out = MessageOut(
                session_id=msg_in.session_id,
                channel=msg_in.channel,
                body=MessageBody(
                    content=content,
                    token_usage=final_usage,
                ),
                origin=msg_in.origin,
                # turn_id 让 channel 端能把最终消息精确匹配到触发它的那一轮，
                # 避免定时任务结果与用户回复在 FIFO 队列里错位
                turn_id=msg_in.id,
                channel_session_id=msg_in.channel_session_id,
            )
            SessionStore.append(msg_out)
            await self.msg_bus.out_queue.enqueue(msg_out)
