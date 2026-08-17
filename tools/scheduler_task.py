"""
Tool for creating scheduled tasks (one-shot or recurring via cron).

Lets the agent schedule a self-contained instruction to run later; the
Scheduler engine dispatches it, and the result is routed back to the
originating session via the injected ToolContext.
"""
from datetime import datetime, timedelta

from datacls import ScheduleTask, ToolContext
from session.scheduler import SchedulerStore
from tools.base import Tool
from utils.cron import next_run


class CreateScheduledTask(Tool):

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
            "08:00). The instruction should be self-contained since it runs "
            "with minimal conversation history."
        )

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "instruction": {
                    "type": "string",
                    "description": (
                        "Self-contained task instruction to execute later. "
                        "Spell out everything needed, as it runs without conversation history."
                    ),
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

        schedule_type = kwargs.get("schedule_type", "once")
        now = datetime.now()

        if schedule_type == "once":
            if kwargs.get("trigger_at_iso"):
                try:
                    trigger_at = datetime.fromisoformat(kwargs["trigger_at_iso"])
                except ValueError as e:
                    return {"success": False, "error": f"Invalid trigger_at_iso: {e}"}
            else:
                minutes = int(kwargs.get("trigger_in_minutes") or 0)
                if minutes < 1:
                    return {
                        "success": False,
                        "error": "For a one-shot task, provide trigger_in_minutes (>=1) or trigger_at_iso.",
                    }
                trigger_at = now + timedelta(minutes=minutes)
            cron = ""
        elif schedule_type == "recurring":
            cron = kwargs.get("cron", "")
            try:
                # 校验表达式，并保证下一次触发严格晚于当前时刻
                trigger_at = next_run(cron, now)
            except ValueError as e:
                return {"success": False, "error": f"Invalid cron expression: {e}"}
        else:
            return {"success": False, "error": f"Unsupported schedule_type: {schedule_type!r}"}

        # 渠道专有路由字段（飞书的 chat_id 等）随创建时的上下文 metadata 一并保存
        task = ScheduleTask(
            channel=context.channel,
            session_id=context.session_id,
            instruction=f'[Scheduled Task]{instruction}',
            schedule_type=schedule_type,
            trigger_at=trigger_at,
            cron=cron,
            status="pending",
            metadata=dict(context.metadata),
        )
        SchedulerStore.create(task)

        return {
            "success": True,
            "task_id": task.id,
            "schedule_type": schedule_type,
            "trigger_at": trigger_at.isoformat(),
            "cron": cron,
            "instruction": instruction,
            "message": f"Scheduled task created. It will run at {trigger_at.isoformat()}.",
        }