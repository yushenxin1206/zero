"""
Scheduler engine.

Runs as an asyncio background task. Polls the SchedulerStore for due tasks
every POLL_INTERVAL_SECONDS. For each due task it enqueues a trigger message
into the incoming bus; the AgentLoop processes it like a normal message, so
the result is routed back to the originating session (via channel_session_id)
with the session's conversation history.

For recurring tasks, after dispatch the next trigger_at is computed from the
cron expression and the task stays pending. For once tasks the terminal status
is written by ``finish_turn`` after the agent loop reports how the run ended;
dispatch itself only marks the task running.
"""
import asyncio
import logging
from datetime import datetime

from zero.bus.queue import BusQueueFull, MessageBus
from zero.models import MessageBody, MessageIn, Origin
from zero.scheduler.models import RunOutcome, ScheduleTask
from zero.scheduler.cron import next_run
from zero.scheduler.store import SchedulerStore

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 10

# 轮次结束时的 loop 状态 → 执行结果
_RUN_OUTCOME: dict[str, RunOutcome] = {
    "completed": "ok",
    "error": "failed",
    "cancelled": "cancelled",
}


class Scheduler:

    def __init__(self, msg_bus: MessageBus, store: SchedulerStore):
        self.msg_bus = msg_bus
        # 任务存储由组合根显式注入；轮询/回写都走这个实例
        self.store = store
        self._running = False
        # 已派发但轮次尚未结束的任务。find_due 仍包含 running（崩溃后重启
        # 要能重跑），进程内用这个集合阻止同一次执行被重复派发。
        self._in_flight: set[str] = set()

    async def run(self) -> None:
        self._running = True
        logger.info("Scheduler started (poll interval=%ss)", POLL_INTERVAL_SECONDS)
        while self._running:
            try:
                now = datetime.now()
                for task in self.store.find_due(now):
                    await self._dispatch(task, now)
            except Exception:
                logger.exception("Scheduler poll iteration failed")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def stop(self) -> None:
        self._running = False

    async def _dispatch(self, task: ScheduleTask, now: datetime) -> None:
        if task.id in self._in_flight:
            # 上一次派发还没执行完：running 会持续出现在 find_due 里
            return
        logger.info("Dispatching scheduled task id=%s type=%s at %s", task.id, task.schedule_type, now.isoformat())

        # 先入队再置 running：入队被拒时任务保持原状态，下一轮轮询自然重试。
        # 反过来会留下「running 但从未派发」的僵尸任务，且本轮 cron 已推进，
        # 这次触发就永久丢了。
        try:
            self.msg_bus.in_queue.enqueue_nowait(
                MessageIn(
                    session_id=task.session_id,
                    channel=task.channel,
                    body=MessageBody(content=task.instruction),
                    origin=Origin(kind="scheduler", task_id=task.id),
                    channel_session_id=task.channel_session_id,
                )
            )
        except BusQueueFull:
            logger.warning(
                "入站队列已满，定时任务 %s 本轮未派发，下轮重试", task.id)
            return
        self._in_flight.add(task.id)
        self.store.update_status(task.id, "running")

        if task.schedule_type == "recurring":
            # 推进到 cron 的下一次触发，任务回到 pending 继续参与调度
            try:
                next_trigger = next_run(task.cron, now)
            except ValueError as e:
                logger.warning("Recurring task %s invalid cron: %s", task.id, e)
                self.store.update_status(task.id, "failed")
                return
            self.store.reschedule(
                task.id, trigger_at=next_trigger, last_run_at=now)
        # once：终态等 finish_turn 回写，派发只代表"已开始"

    async def finish_turn(self, msg_in: MessageIn, status: str,
                          error: str) -> None:
        """AgentLoop 轮次结束回调：把真实结果回写给定时任务。

        once 在这一刻才进入 done/failed/cancelled；recurring 保持 pending，
        只记录本轮 last_status / last_error。非定时任务来源直接忽略。
        """
        origin = msg_in.origin
        if origin.kind != "scheduler" or not origin.task_id:
            return
        task_id = origin.task_id
        self._in_flight.discard(task_id)
        outcome = _RUN_OUTCOME.get(status, "failed")
        await asyncio.to_thread(
            self.store.finish_run, task_id, outcome=outcome,
            error="" if outcome == "ok" else error)
