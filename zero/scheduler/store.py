"""
Scheduled task storage.

Persists scheduled tasks as JSONL:
    ~/.zero/scheduler/tasks.jsonl
one ScheduleTask per line, keyed by task id.
"""
from datetime import datetime

from zero.paths import current
from zero.scheduler.models import ScheduleTask, TaskStatus


class SchedulerStore:

    @staticmethod
    def _load_tasks() -> dict[str, ScheduleTask]:
        tasks: dict[str, ScheduleTask] = {}
        path = current().tasks
        if not path.exists():
            return tasks
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                task = ScheduleTask.model_validate_json(line)
                tasks[task.id] = task
        return tasks

    @staticmethod
    def _save_tasks(tasks: dict[str, ScheduleTask]) -> None:
        path = current().tasks
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for task in tasks.values():
                f.write(task.model_dump_json() + "\n")

    @staticmethod
    def create(task: ScheduleTask) -> ScheduleTask:
        tasks = SchedulerStore._load_tasks()
        tasks[task.id] = task
        SchedulerStore._save_tasks(tasks)
        return task

    @staticmethod
    def get(task_id: str) -> ScheduleTask | None:
        return SchedulerStore._load_tasks().get(task_id)

    @staticmethod
    def load_all() -> list[ScheduleTask]:
        return list(SchedulerStore._load_tasks().values())

    @staticmethod
    def find_due(now: datetime) -> list[ScheduleTask]:
        due: list[ScheduleTask] = []
        for task in SchedulerStore._load_tasks().values():
            if task.status not in ("pending", "running"):
                continue
            if task.trigger_at is not None and task.trigger_at <= now:
                due.append(task)
        return due

    @staticmethod
    def update_status(
            task_id: str, status: TaskStatus,
            last_run_at: datetime | None = None) -> None:
        tasks = SchedulerStore._load_tasks()
        task = tasks.get(task_id)
        if task is None:
            return
        task.status = status
        if last_run_at is not None:
            task.last_run_at = last_run_at
        task.updated_at = datetime.now()
        SchedulerStore._save_tasks(tasks)

    @staticmethod
    def reschedule(task_id: str, trigger_at: datetime, last_run_at: datetime) -> None:
        """Recurring：执行后把 trigger_at 推进到 cron 的下一次触发时间。"""
        tasks = SchedulerStore._load_tasks()
        task = tasks.get(task_id)
        if task is None:
            return
        task.trigger_at = trigger_at
        task.last_run_at = last_run_at
        task.status = "pending"
        task.updated_at = datetime.now()
        SchedulerStore._save_tasks(tasks)
