"""
shell 命令的就地审批请求构造。

设计要点：
- hook handler 运行在 agent loop 内，无法 yield 流式事件，所以直接
  向 bus.stream_queue 入队一条 kind="approval" 的 StreamEvent，并自行
  盖章 channel / session_id / turn_id（turn_id 由 AgentLoop 经
  ToolContext 顶层字段注入），让 CLI 的 turn 路由能正确认领；
- 事件 content 是通用的 ApprovalRequest 协议（models.py），channel 端
  按协议渲染通用审批面板，不认识 shell_guard；业务文案（选项、超时
  引导）全部由本模块填好；
- 答案经 bus.approvals 进程内 Future 回填（无需经总线回程）；
- deadline（unix 时间戳）由本端统一签发，CLI 按它做单键倒计时——
  前后端共用一个权威时点，不存在两端各自计时导致的竞态；
- 超时或无人认领（后台 turn、渲染被 Ctrl+C 中断）→ 返回 None，
  由 handler  fail-closed。
"""
import asyncio
import logging
import time
from uuid import uuid4

from zero.bus.queue import MessageBus
from zero.core.cancellation import TurnCancelled
from zero.models import ApprovalOption, ApprovalRequest, StreamEvent

logger = logging.getLogger(__name__)

# 等待用户作答的最长时间；超时按拒绝处理
TIMEOUT = 180

ANSWERS = ("y", "a", "s", "t", "n", "d")  # 允许一次 / 总是允许 / 会话放行 / 本轮放行 / 拒绝 / 总是拒绝

_OPTIONS = [
    ApprovalOption(key="y", label="允许一次"),
    ApprovalOption(key="a", label="总是允许(该命令)"),
    ApprovalOption(key="s", label="本会话放行"),
    ApprovalOption(key="t", label="本轮放行"),
    ApprovalOption(key="n", label="拒绝"),
    ApprovalOption(key="d", label="总是拒绝"),
]

_TIMEOUT_HINT = (
    "该命令未被执行。如需免审批放行，可预置规则："
    "zero-guard add allow \"<pattern>\""
)


async def request(
        bus: MessageBus,
        *,
        channel: str,
        session_id: str,
        turn_id: str,
        command: str,
        reason: str,
        stop_event: asyncio.Event | None = None,
) -> str | None:
    """发出审批请求并等待答案。返回答案（y/n/a/w）或 None（超时/已终止）。"""
    req_id = uuid4().hex[:12]
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    bus.approvals.register(req_id, fut)
    try:
        req = ApprovalRequest(
            req_id=req_id,
            title="shell 命令审批",
            summary=command,
            detail=reason,
            options=_OPTIONS,
            timeout_hint=_TIMEOUT_HINT,
            deadline=time.time() + TIMEOUT,
        )
        event = StreamEvent(
            channel=channel,
            session_id=session_id,
            turn_id=turn_id,
            kind="approval",
            content=req.model_dump_json(),
        )
        await bus.stream_queue.enqueue(event)
        if stop_event is not None:
            # 用户终止竞争：审批等待是最长的一个 await 点（TIMEOUT 秒），
            # 不竞争的话 stop 置位后 runner 仍挂在这里直到超时。stop 胜出
            # 则抛 TurnCancelled 走正常收尾，approval future 保持未决、
            # 由 finally 丢弃。
            stop_task = asyncio.create_task(stop_event.wait())
            try:
                done, _pending = await asyncio.wait(
                    {fut, stop_task}, timeout=TIMEOUT,
                    return_when=asyncio.FIRST_COMPLETED)
            finally:
                stop_task.cancel()
            if stop_event.is_set():
                raise TurnCancelled()
            if fut in done:
                return fut.result()
            logger.info("shell_guard approval %s timed out", req_id)
            return None
        return await asyncio.wait_for(fut, timeout=TIMEOUT)
    except (asyncio.TimeoutError, TimeoutError):
        logger.info("shell_guard approval %s timed out", req_id)
        return None
    finally:
        bus.approvals.discard(req_id)
