"""
Domain logic for creating scheduled tasks.

Computes the first trigger time (one-shot delay / ISO datetime, or the next
cron occurrence) and persists the task via SchedulerStore. The
CreateScheduledTask tool is a thin wrapper around this module.
"""
from datetime import datetime, timedelta

from zero.scheduler.models import (
    SCHEDULE_ONCE, SCHEDULE_RECURRING, TASK_CANCELLED, TASK_COMPLETED,
    TASK_FAILED, TASK_PENDING, ScheduleTask, ScheduleType)
from zero.scheduler.cron import next_run
from zero.scheduler.store import SchedulerStore


def create_task(
    store: SchedulerStore,
    *,
    instruction: str,
    schedule_type: ScheduleType,
    channel: str,
    session_id: str,
    channel_session_id: str = "",
    trigger_in_minutes: int | None = None,
    trigger_at_iso: str | None = None,
    cron: str = "",
) -> ScheduleTask:
    """计算首次触发时间并持久化任务，返回存储后的 ScheduleTask。

    参数不合法时抛出 ValueError，由调用方（如工具层）决定如何呈现错误。
    """
    now = datetime.now()

    if schedule_type == SCHEDULE_ONCE:
        if trigger_at_iso:
            try:
                trigger_at = datetime.fromisoformat(trigger_at_iso)
            except ValueError as e:
                raise ValueError(f"Invalid trigger_at_iso: {e}") from e
        else:
            minutes = int(trigger_in_minutes or 0)
            if minutes < 1:
                raise ValueError(
                    "For a one-shot task, provide trigger_in_minutes (>=1) or trigger_at_iso."
                )
            trigger_at = now + timedelta(minutes=minutes)
        cron = ""
    elif schedule_type == SCHEDULE_RECURRING:
        try:
            # 校验表达式，并保证下一次触发严格晚于当前时刻
            trigger_at = next_run(cron, now)
        except ValueError as e:
            raise ValueError(f"Invalid cron expression: {e}") from e
    else:
        raise ValueError(f"Unsupported schedule_type: {schedule_type!r}")

    # 渠道侧会话标识随任务保存，触发时挂到 MessageIn 用于结果回投
    task = ScheduleTask(
        channel=channel,
        session_id=session_id,
        instruction=f'[Scheduled Task]{instruction}',
        schedule_type=schedule_type,
        trigger_at=trigger_at,
        cron=cron,
        status=TASK_PENDING,
        channel_session_id=channel_session_id,
    )
    store.create(task)
    return task


def cancel_task(
        store: SchedulerStore, *, task_id: str,
        session_id: str) -> ScheduleTask:
    """取消一个尚未终结（completed/failed/cancelled）的定时任务。

    仅允许取消属于 session_id 的任务；找不到或状态不允许时抛出 ValueError，
    由调用方（如工具层）决定如何呈现错误。
    """
    task = store.get(task_id)
    if task is None:
        raise ValueError(f"Scheduled task not found: {task_id!r}")
    if task.session_id != session_id:
        raise ValueError(f"Scheduled task {task_id!r} does not belong to this session.")
    if task.status in (TASK_COMPLETED, TASK_FAILED, TASK_CANCELLED):
        raise ValueError(f"Scheduled task {task_id!r} is already {task.status}, cannot cancel.")

    store.update_status(task_id, TASK_CANCELLED)
    task.status = TASK_CANCELLED
    return task
