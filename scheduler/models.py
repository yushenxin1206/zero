"""
Scheduler-specific data models.
"""

from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import Field

from models import Base


class ScheduleTask(Base):
    """A persisted scheduled task to be executed by the Scheduler engine."""
    id: str = Field(default_factory=lambda: str(uuid4()))
    channel: str = ""
    session_id: str = ""  # 目标会话，结果回投到该会话
    instruction: str = ""  # 创建时固化的自包含指令
    schedule_type: str = "once"  # once | recurring
    trigger_at: datetime | None = None  # once: 执行时刻；recurring: 下次执行时刻
    cron: str = ""  # recurring: 5 字段 cron 表达式
    status: str = "pending"  # pending | running | done | failed | cancelled
    # 渠道专有路由字段（如飞书的 chat_id）放在 metadata
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    last_run_at: datetime | None = None
