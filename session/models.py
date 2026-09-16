"""
Session-specific data models.
"""

from datetime import datetime
from uuid import uuid4

from pydantic import Field

from models import Base


class SessionMeta(Base):
    """会话索引条目（~/.zero/sessions/{channel}/index.json 的 value）。

    索引结构为 dict[source_id, SessionMeta]。旧索引缺少的字段（如 title）
    由默认值兜底；未知字段校验时忽略，磁盘格式保持 snake_case 不变。
    """
    session_id: str = Field(default_factory=lambda: str(uuid4()))
    date: str = ""  # 创建日期 YYYY-MM-DD，用于会话目录分桶
    last_updated_at: datetime = Field(default_factory=datetime.now)
    title: str = ""
    # 索引的 key，仅 list_sessions 返回时回填，不落盘
    source_id: str = Field(default="", exclude=True)
