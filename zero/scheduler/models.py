"""
Scheduler-specific data models.
"""

from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import Field

from zero.models import Base

ScheduleType = Literal["once", "recurring"]
TaskStatus = Literal["pending", "running", "done", "failed", "cancelled"]
# 一轮实际执行的结果；与任务状态区分：recurring 执行失败后任务仍回到
# pending 等待下一次触发，失败事实记录在 last_status / last_error 上。
RunOutcome = Literal["ok", "failed", "cancelled"]


class ScheduleTask(Base):
    """A persisted scheduled task to be executed by the Scheduler engine."""
    id: str = Field(default_factory=lambda: str(uuid4()))
    channel: str = ""
    session_id: str = ""  # 目标会话，结果回投到该会话
    instruction: str = ""  # 创建时固化的自包含指令
    schedule_type: ScheduleType = "once"
    trigger_at: datetime | None = None  # once: 执行时刻；recurring: 下次执行时刻
    cron: str = ""  # recurring: 5 字段 cron 表达式
    status: TaskStatus = "pending"
    # 渠道侧会话标识（飞书 chat_id、cli source_id），结果回投寻址用
    channel_session_id: str = ""
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    last_run_at: datetime | None = None
    # 最近一轮真实执行的结果与失败原因（空串表示还没跑过）
    last_status: RunOutcome | Literal[""] = ""
    last_error: str = ""
