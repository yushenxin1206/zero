"""定时任务完成态：派发只置 running，轮次真正结束才回写终态。"""
import asyncio
from datetime import datetime, timedelta

from zero.bus.queue import MessageBus
from zero.models import MessageBody, MessageIn, Origin
from zero.scheduler.engine import Scheduler
from zero.scheduler.models import ScheduleTask
from zero.scheduler.store import SchedulerStore


def _store(tmp_path) -> SchedulerStore:
    return SchedulerStore(tmp_path / "scheduler" / "tasks.jsonl")


def _task(store: SchedulerStore, **kwargs) -> ScheduleTask:
    task = ScheduleTask(
        channel="cli",
        session_id="s1",
        instruction="do it",
        trigger_at=datetime.now() - timedelta(seconds=1),
        **kwargs,
    )
    return store.create(task)


def _finished(task: ScheduleTask) -> MessageIn:
    return MessageIn(
        session_id=task.session_id,
        channel=task.channel,
        body=MessageBody(content=task.instruction),
        origin=Origin(kind="scheduler", task_id=task.id),
    )


def test_once_task_finishes_only_after_turn(tmp_path):
    store = _store(tmp_path)
    task = _task(store)
    scheduler = Scheduler(MessageBus(), store)

    asyncio.run(scheduler._dispatch(task, datetime.now()))
    assert store.get(task.id).status == "running"

    asyncio.run(scheduler.finish_turn(_finished(task), "completed", ""))
    saved = store.get(task.id)
    assert saved.status == "done"
    assert saved.last_status == "ok"
    assert saved.last_run_at is not None


def test_once_task_failure_writes_reason(tmp_path):
    store = _store(tmp_path)
    task = _task(store)
    scheduler = Scheduler(MessageBus(), store)

    asyncio.run(scheduler._dispatch(task, datetime.now()))
    asyncio.run(scheduler.finish_turn(_finished(task), "error", "boom"))
    saved = store.get(task.id)
    assert saved.status == "failed"
    assert saved.last_status == "failed"
    assert saved.last_error == "boom"


def test_recurring_task_stays_pending_but_records_outcome(tmp_path):
    store = _store(tmp_path)
    task = _task(store, schedule_type="recurring", cron="* * * * *")
    scheduler = Scheduler(MessageBus(), store)

    asyncio.run(scheduler._dispatch(task, datetime.now()))
    assert store.get(task.id).status == "pending"

    asyncio.run(scheduler.finish_turn(_finished(task), "error", "boom"))
    saved = store.get(task.id)
    assert saved.status == "pending"
    assert saved.last_status == "failed"
    assert saved.last_error == "boom"


def test_running_task_is_not_dispatched_twice(tmp_path):
    store = _store(tmp_path)
    task = _task(store)
    scheduler = Scheduler(MessageBus(), store)

    now = datetime.now()
    asyncio.run(scheduler._dispatch(task, now))
    asyncio.run(scheduler._dispatch(task, now))
    assert scheduler.msg_bus.in_queue.queue.qsize() == 1
