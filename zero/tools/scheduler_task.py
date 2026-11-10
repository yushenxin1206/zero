"""
Tools for scheduled tasks: creating (one-shot or recurring via cron) and cancelling.

Thin wrappers around scheduler.service: extract arguments from kwargs and the
injected ToolContext, then format the result dict. Lets the agent schedule a
self-contained instruction to run later (the Scheduler engine dispatches it,
and the result is routed back to the originating session), or cancel a task
that has not finished yet.
"""
from zero.models import ToolContext
from zero.scheduler.models import SCHEDULE_ONCE
from zero.scheduler.service import cancel_task, create_task
from zero.scheduler.store import SchedulerStore
from zero.tools.base import Tool


class CreateScheduledTask(Tool):

    def __init__(self, *, store: SchedulerStore):
        # 任务存储由组合根注入，工具只做参数解析与结果格式化
        self.store = store

    @property
    def name(self):
        return "create_scheduled_task"

    @property
    def description(self):
        return (
            "Create a scheduled task that runs an instruction at a later time "
            "and reports the result back to the current conversation. Accepts "
            "either a one-shot delay (trigger_in_minutes / trigger_at_iso) or "
            "a recurring cron expression (cron, e.g. '0 8 * * *' for daily at "
            "08:00). The instruction runs with the session's conversation "
            "history available."
        )

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "instruction": {
                    "type": "string",
                    "description": "Task instruction to execute later.",
                },
                "schedule_type": {
                    "type": "string",
                    "enum": ["once", "recurring"],
                    "description": "'once' for a one-shot task, 'recurring' for a cron task.",
                },
                "trigger_in_minutes": {
                    "type": "integer",
                    "description": "For 'once': run after this many minutes from now.",
                    "minimum": 1,
                },
                "trigger_at_iso": {
                    "type": "string",
                    "description": "For 'once': absolute ISO datetime to run (alternative to trigger_in_minutes).",
                },
                "cron": {
                    "type": "string",
                    "description": "For 'recurring': 5-field cron expression, e.g. '0 8 * * *'.",
                },
            },
            "required": ["instruction", "schedule_type"],
        }

    async def run(self, context: ToolContext | None = None, **kwargs):
        if context is None or not context.session_id:
            return {"success": False, "error": "No session context available for scheduled task."}

        instruction = (kwargs.get("instruction") or "").strip()
        if not instruction:
            return {"success": False, "error": "Missing required argument: instruction."}

        try:
            task = create_task(
                self.store,
                instruction=instruction,
                schedule_type=kwargs.get("schedule_type", SCHEDULE_ONCE),
                channel=context.channel,
                session_id=context.session_id,
                channel_session_id=context.channel_session_id,
                trigger_in_minutes=kwargs.get("trigger_in_minutes"),
                trigger_at_iso=kwargs.get("trigger_at_iso"),
                cron=kwargs.get("cron", ""),
            )
        except ValueError as e:
            return {"success": False, "error": str(e)}

        return {
            "success": True,
            "task_id": task.id,
            "schedule_type": task.schedule_type,
            "trigger_at": task.trigger_at.isoformat(),
            "cron": task.cron,
            "instruction": instruction,
            "message": f"Scheduled task created. It will run at {task.trigger_at.isoformat()}.",
        }


class CancelScheduledTask(Tool):

    def __init__(self, *, store: SchedulerStore):
        self.store = store

    @property
    def name(self):
        return "cancel_scheduled_task"

    @property
    def description(self):
        return (
            "Cancel a scheduled task by its task_id so it will no longer run. "
            "Only pending or running tasks belonging to the current session "
            "can be cancelled; use the task_id returned when the task was created."
        )

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task_id returned when the scheduled task was created.",
                },
            },
            "required": ["task_id"],
        }

    async def run(self, context: ToolContext | None = None, **kwargs):
        if context is None or not context.session_id:
            return {"success": False, "error": "No session context available for scheduled task."}

        task_id = (kwargs.get("task_id") or "").strip()
        if not task_id:
            return {"success": False, "error": "Missing required argument: task_id."}

        try:
            task = cancel_task(
                self.store, task_id=task_id, session_id=context.session_id)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        return {
            "success": True,
            "task_id": task.id,
            "status": "cancelled",
            "message": f"Scheduled task {task.id} cancelled.",
        }
