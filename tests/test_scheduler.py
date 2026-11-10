"""定时任务完成态：派发只置 running，轮次真正结束才回写终态。"""
import asyncio
import logging
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


def test_finish_turn_logs_write_failure(tmp_path, caplog, monkeypatch):
    """写盘失败无法落任何标记：任务停在 running，至少留下带 task id 的日志。"""
    store = _store(tmp_path)
    task = _task(store)
    scheduler = Scheduler(MessageBus(), store)
    asyncio.run(scheduler._dispatch(task, datetime.now()))

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store, "finish_run", fail)
    with caplog.at_level(logging.ERROR, logger="zero.scheduler.engine"):
        asyncio.run(scheduler.finish_turn(_finished(task), "completed", ""))

    assert any(
        task.id in record.getMessage() and "回写失败" in record.getMessage()
        for record in caplog.records)
    assert store.get(task.id).status == "running"


def test_finish_run_missing_task_logs_warning(tmp_path, caplog):
    store = _store(tmp_path)
    with caplog.at_level(logging.WARNING, logger="zero.scheduler.store"):
        store.finish_run("missing", outcome="ok")
    assert "不存在" in caplog.text
