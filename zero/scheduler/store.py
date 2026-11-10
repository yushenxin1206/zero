"""
Scheduled task storage.

Persists scheduled tasks as JSONL:
    <data root>/scheduler/tasks.jsonl
one ScheduleTask per line, keyed by task id.

构造时接收任务文件路径，不读进程级路径；由组合根（main）创建并注入。
"""
from datetime import datetime
import logging
from pathlib import Path

from zero.scheduler.models import RunOutcome, ScheduleTask, TaskStatus

logger = logging.getLogger(__name__)

# once 任务执行结果 → 终态。显式标注键值类型：内联字典会被推断成
# dict[str, str]，取出来是无法赋给 TaskStatus（Literal）的裸 str。
_ONCE_FINAL_STATUS: dict[RunOutcome, TaskStatus] = {
    "ok": "done",
    "failed": "failed",
    "cancelled": "cancelled",
}


class SchedulerStore:

    def __init__(self, tasks_file: Path):
        self.tasks_file = Path(tasks_file)

    def _load_tasks(self) -> dict[str, ScheduleTask]:
        tasks: dict[str, ScheduleTask] = {}
        path = self.tasks_file
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

    def _save_tasks(self, tasks: dict[str, ScheduleTask]) -> None:
        path = self.tasks_file
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for task in tasks.values():
                f.write(task.model_dump_json() + "\n")

    def create(self, task: ScheduleTask) -> ScheduleTask:
        tasks = self._load_tasks()
        tasks[task.id] = task
        self._save_tasks(tasks)
        return task

    def get(self, task_id: str) -> ScheduleTask | None:
        return self._load_tasks().get(task_id)

    def load_all(self) -> list[ScheduleTask]:
        return list(self._load_tasks().values())

    def find_due(self, now: datetime) -> list[ScheduleTask]:
        due: list[ScheduleTask] = []
        for task in self._load_tasks().values():
            if task.status not in ("pending", "running"):
                continue
            if task.trigger_at is not None and task.trigger_at <= now:
                due.append(task)
        return due

    def update_status(
            self, task_id: str, status: TaskStatus,
            last_run_at: datetime | None = None) -> None:
        tasks = self._load_tasks()
        task = tasks.get(task_id)
        if task is None:
            return
        task.status = status
        if last_run_at is not None:
            task.last_run_at = last_run_at
        task.updated_at = datetime.now()
        self._save_tasks(tasks)

    def reschedule(self, task_id: str, trigger_at: datetime,
                   last_run_at: datetime) -> None:
        """Recurring：执行后把 trigger_at 推进到 cron 的下一次触发时间。"""
        tasks = self._load_tasks()
        task = tasks.get(task_id)
        if task is None:
            return
        task.trigger_at = trigger_at
        task.last_run_at = last_run_at
        task.status = "pending"
        task.updated_at = datetime.now()
        self._save_tasks(tasks)

    def finish_run(self, task_id: str, *, outcome: RunOutcome,
                   error: str = "") -> None:
        """一轮真正结束后回写结果。

        once 在这一刻才进入终态（done / failed / cancelled）——派发入队
        只是"已开始"，不是"已完成"。recurring 已由 dispatch 推进到下一次
        触发并回到 pending，这里只记录本轮结果，不覆盖任务状态。

        进程在轮次中途崩溃时，once 会停留在 running，重启后仍会被
        find_due 捞出重跑（at-least-once），不会永久卡住。

        写盘失败时任务同样停留在 running：同一份存储写不进去，再写一条
        "失败"标记大概率也不会成功，所以这里不做假回写，只让异常向上抛，
        由调用方带 task id 记录错误日志。这是本兜底唯一的可见痕迹。
        """
        tasks = self._load_tasks()
        task = tasks.get(task_id)
        if task is None:
            logger.warning(
                "定时任务 %s 不存在，忽略本轮结果回写（outcome=%s）",
                task_id, outcome)
            return
        task.last_status = outcome
        task.last_error = "" if outcome == "ok" else error
        task.last_run_at = datetime.now()
        if task.schedule_type == "once":
            task.status = _ONCE_FINAL_STATUS[outcome]
        task.updated_at = datetime.now()
        self._save_tasks(tasks)
