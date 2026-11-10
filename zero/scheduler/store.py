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

from zero.scheduler.models import SCHEDULE_ONCE, ScheduleTask, TaskStatus

logger = logging.getLogger(__name__)


class SchedulerStore:

    def __init__(self, tasks_file: Path):
        self.tasks_file = Path(tasks_file)

    def _load_tasks(self) -> dict[str, ScheduleTask]:
        tasks: dict[str, ScheduleTask] = {}
        path = self.tasks_file
        if not path.exists():
            return tasks
        with path.open("r", encoding="utf-8") as f:
            for line_number, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    task = ScheduleTask.model_validate_json(line)
                except ValueError:
                    # 单条坏数据（含旧版 status="done" 这类已失效取值）只
                    # 丢弃该条，不能让整个任务列表加载失败。下一次写盘时
                    # 该行会随全量重写消失。
                    logger.warning(
                        "跳过无法解析的定时任务（%s 第 %d 行）：%s",
                        path, line_number, line[:120])
                    continue
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

    def finish_run(self, task_id: str, *, status: TaskStatus) -> None:
        """一轮真正结束后回写任务状态。

        once 在这一刻才进入终态（completed / failed / cancelled）——派发
        入队只是"已开始"，不是"已完成"。recurring 已由 dispatch 推进到
        下一次触发并回到 pending，这里只更新 last_run_at；本轮成败不落盘，
        由轮次摘要日志记录。

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
                "定时任务 %s 不存在，忽略本轮状态回写（status=%s）",
                task_id, status)
            return
        task.last_run_at = datetime.now()
        if task.schedule_type == SCHEDULE_ONCE:
            task.status = status
        task.updated_at = datetime.now()
        self._save_tasks(tasks)
