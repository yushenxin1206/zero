"""
子 agent 并行调度器：后台执行 + 结果回投。

主对话通过 subagent 工具提交一批相互独立的任务，manager 为每个任务起一个
asyncio task（信号量限制并发），在隔离的 SubAgentContext 中单次执行：不落
任何会话历史、不审批（hooks=None）、工具集为裁剪后的子集（无 shell / 无递归
subagent / 无定时任务）。

每个子任务完成后，把「任务指令 + 结果」作为一条新的 MessageIn 回投到
bus.in_queue——AgentLoop 按普通消息处理，走一次常规 turn 并把最终 MessageOut
路由回父会话（复用定时任务回投的同一条路径，见 scheduler/engine.py）。
"""
import asyncio
import itertools
import logging

from zero.bus.queue import MessageBus
from zero.config import Config
from zero.core.runner import run_subagent
from zero.models import (
    MessageBody, MessageIn, Origin, StreamEvent, ToolContext)
from zero.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# 同时在跑的子 agent 上限：避免一批任务把 provider 连接/限流打满
DEFAULT_MAX_CONCURRENCY = 4


class SubagentManager:
    """子 agent 并行调度与回投。进程内单例，生命周期与 MessageBus 一致。"""

    def __init__(
            self,
            msg_bus: MessageBus,
            config: Config,
            tool_registry: ToolRegistry,
            max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    ):
        self.msg_bus = msg_bus
        self.config = config
        # 裁剪后的子 agent 工具集（调用方用 ToolRegistry.subset 构造）
        self.tool_registry = tool_registry
        self._sem = asyncio.Semaphore(max_concurrency)
        # 在途子任务：id -> asyncio.Task，shutdown 时据此取消
        self._tasks: dict[str, asyncio.Task] = {}
        self._seq = itertools.count(1)

    def launch(
            self, tasks: list[dict], *, tool_context: ToolContext) -> list[dict]:
        """为每个任务创建后台协程并立即返回（不等待执行）。

        必须在事件循环内调用（工具 run 的 async 语境天然满足）。
        返回每个已启动任务的 {id, description}，供工具回给模型。
        """
        launched: list[dict] = []
        for item in tasks:
            prompt = item["prompt"]
            sid = f"sub-{next(self._seq)}"
            desc = (item.get("description") or "").strip()
            self._tasks[sid] = asyncio.create_task(
                self._run_one(sid, prompt, desc, tool_context))
            launched.append({"id": sid, "description": desc or prompt[:60]})
        return launched

    async def _run_one(
            self, sid: str, prompt: str, desc: str,
            tool_context: ToolContext) -> None:
        """执行单个子任务并回投结果。task 名固定为 subagent，走独立运行时
        （可在 agentRuntimes.subagent 绑定与主对话不同的模型）。"""
        try:
            async with self._sem:
                result = await run_subagent(
                    prompt,
                    config=self.config,
                    task="subagent",
                    tool_registry=self.tool_registry,
                    hooks=None,  # 子 agent 行为不需要审批
                    tool_context=tool_context,
                )
        except asyncio.CancelledError:
            # 进程退出/显式取消：不回投（会话可能已收摊）
            raise
        except Exception as e:  # noqa: BLE001 - 单个子任务失败不应拖垮整体
            logger.exception("Subagent %s failed", sid)
            result = f"（子任务执行失败：{e}）"
        finally:
            self._tasks.pop(sid, None)
        if not result.strip():
            # 空回复会以「结果为空」回投，主对话只能误解或重跑。明确标注，
            # 常见成因：模型思考吃满输出预算被截断（只产出 reasoning）。
            result = "（子任务未产出任何内容，可能是模型输出被截断或仅产出思考）"
        await self._emit_result(sid, result, tool_context)
        await self._dispatch_back(sid, desc, result, tool_context)

    async def _emit_result(
            self, sid: str, result: str, tool_context: ToolContext) -> None:
        """把子任务结果原文作为独立流事件直接发给 channel，供其原样展示。

        与回投是两条独立通道：回投（_dispatch_back）让主对话据结果继续，
        本事件让用户直接看到原文，不必依赖主对话转述。turn_id 用子任务 id，
        channel 端据此识别为后台来源、不误当当前交互轮。
        """
        await self.msg_bus.stream_queue.enqueue(StreamEvent(
            channel=tool_context.channel,
            session_id=tool_context.session_id,
            turn_id=sid,
            kind="subagent_result",
            content=result,
            origin=Origin(kind="subagent", task_id=sid),
        ))

    async def _dispatch_back(
            self, sid: str, desc: str, result: str,
            tool_context: ToolContext) -> None:
        """把子任务结果作为一条新消息回投到入站总线。

        路由字段（channel / session_id / channel_session_id 等）继承父轮的
        ToolContext，AgentLoop 处理后把最终回复送回父会话；origin 标记为
        subagent 并带上子任务 id，供 channel 端区分后台结果来源。
        """
        await self.msg_bus.in_queue.enqueue(
            MessageIn(
                session_id=tool_context.session_id,
                channel=tool_context.channel,
                body=MessageBody(content=_render_back(sid, desc, result)),
                origin=Origin(kind="subagent", task_id=sid),
                channel_message_id=tool_context.channel_message_id,
                channel_sender_id=tool_context.channel_sender_id,
                channel_session_id=tool_context.channel_session_id,
            )
        )

    async def shutdown(self) -> None:
        """取消所有在途子任务并等待收敛（进程退出时调用）。"""
        pending = [t for t in self._tasks.values() if not t.done()]
        for t in pending:
            t.cancel()
        for t in pending:
            try:
                await t
            except BaseException:  # noqa: BLE001 - 收尾阶段吞掉取消/失败
                pass
        self._tasks.clear()


def _render_back(sid: str, desc: str, result: str) -> str:
    """回投消息正文：明确告知这是已完成的后台子任务，避免主对话重跑。"""
    label = desc or sid
    return (
        f"[后台子任务 {sid} 已完成] {label}\n\n"
        f"以下是该子任务的执行结果。请用你自己的话向用户转达关键结论，"
        f"不要原文照搬，也不要重新执行该任务：\n\n{result}"
    )