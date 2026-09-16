"""
CLI 就地审批：通过 stream 总线把审批请求路由到当前激活的交互 turn，
CLI 通道收到后暂停 Live 渲染、就地提问、回填 Future。

设计要点：
- hook handler 运行在 agent loop 内，无法 yield 流式事件，所以直接
  向 bus.stream_queue 入队一条 kind="approval" 的 StreamEvent，并自行
  盖章 channel / session_id / turn_id（turn_id 由 AgentLoop 经
  ToolContext.metadata 注入），让 CLI 的 turn 路由能正确认领；
- 答案经内存 Future 回填（进程内，无需经总线回程）；
- 事件 content 里带 deadline（unix 时间戳），CLI 按它做单键倒计时，
  到时自行退出提问并打印超时拦截提示——前后端共用一个权威时点，
  不存在两端各自计时导致的竞态；
- 超时或无人认领（后台 turn、渲染被 Ctrl+C 中断）→ 返回 None，
  由 handler  fail-closed。
"""
import asyncio
import json
import logging
import time
from uuid import uuid4

from bus.queue import MessageBus
from models import StreamEvent

logger = logging.getLogger(__name__)

# 等待用户作答的最长时间；超时按拒绝处理
TIMEOUT = 180

ANSWERS = ("y", "n", "a", "w")  # 允许一次 / 拒绝 / 总是允许 / 总是拒绝

_pending: dict[str, asyncio.Future] = {}


async def request(
        bus: MessageBus,
        *,
        channel: str,
        session_id: str,
        turn_id: str,
        metadata: dict,
        command: str,
        reason: str,
) -> str | None:
    """发出审批请求并等待答案。返回答案（y/n/a/w）或 None（超时）。"""
    req_id = uuid4().hex[:12]
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _pending[req_id] = fut
    try:
        event = StreamEvent(
            channel=channel,
            session_id=session_id,
            turn_id=turn_id,
            kind="approval",
            content=json.dumps(
                {"req_id": req_id, "command": command, "reason": reason,
                 "deadline": time.time() + TIMEOUT},
                ensure_ascii=False,
            ),
            metadata=dict(metadata),
        )
        await bus.stream_queue.enqueue(event)
        return await asyncio.wait_for(fut, timeout=TIMEOUT)
    except (asyncio.TimeoutError, TimeoutError):
        logger.info("shell_guard approval %s timed out", req_id)
        return None
    finally:
        _pending.pop(req_id, None)


def resolve(req_id: str, answer: str) -> bool:
    """CLI 通道回填答案。返回 False 表示请求不存在或已完成。"""
    if answer not in ANSWERS:
        return False
    fut = _pending.get(req_id)
    if fut is None or fut.done():
        return False
    fut.set_result(answer)
    return True
