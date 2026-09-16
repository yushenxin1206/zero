"""
Scheduler engine.

Runs as an asyncio background task. Polls the SchedulerStore for due tasks
every POLL_INTERVAL_SECONDS. For each due task it enqueues a trigger message
into the incoming bus; the AgentLoop processes it like a normal message, so
the result is routed back to the originating session (via metadata.chat_id)
with the session's conversation history.

For recurring tasks, after dispatch the next trigger_at is computed from the
cron expression and the task stays pending.
"""
import asyncio
import logging
from datetime import datetime

from bus.queue import MessageBus
from models import MessageBody, MessageIn
from scheduler.models import ScheduleTask
from scheduler.cron import next_run
from scheduler.store import SchedulerStore

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 10


class Scheduler:

    def __init__(self, msg_bus: MessageBus):
        self.msg_bus = msg_bus
        self._running = False

    async def run(self) -> None:
        self._running = True
        logger.info("Scheduler started (poll interval=%ss)", POLL_INTERVAL_SECONDS)
        while self._running:
            try:
                now = datetime.now()
                for task in SchedulerStore.find_due(now):
                    await self._dispatch(task, now)
            except Exception:
                logger.exception("Scheduler poll iteration failed")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def stop(self) -> None:
        self._running = False

    async def _dispatch(self, task: ScheduleTask, now: datetime) -> None:
        logger.info("Dispatching scheduled task id=%s type=%s at %s", task.id, task.schedule_type, now.isoformat())

        # 标记为正在执行并投递触发消息
        SchedulerStore.update_status(task.id, "running")
        trigger_metadata = {
            "source": "scheduler",
            "task_id": task.id,
            "channel": task.channel,
            **dict(task.metadata or {}),
        }
        await self.msg_bus.in_queue.enqueue(
            MessageIn(
                session_id=task.session_id,
                channel=task.channel,
                body=MessageBody(content=task.instruction),
                metadata=trigger_metadata,
            )
        )

        if task.schedule_type == "recurring":
            # 推进到 cron 的下一次触发，任务回到 pending 继续参与调度
            try:
                next_trigger = next_run(task.cron, now)
            except ValueError as e:
                logger.error("Recurring task %s invalid cron: %s", task.id, e)
                SchedulerStore.update_status(task.id, "failed")
                return
            SchedulerStore.reschedule(task.id, trigger_at=next_trigger, last_run_at=now)
        else:
            SchedulerStore.update_status(task.id, "done", last_run_at=now)
