"""
Session-specific data models.
"""

from datetime import datetime
from uuid import uuid4

from pydantic import Field

from zero.models import Base


class SessionMeta(Base):
    """会话索引条目（~/.zero/sessions/{channel}/index.json 的 value）。

    索引结构为 dict[source_id, SessionMeta]。旧索引缺少的字段（如 title、
    description）由默认值兜底；未知字段校验时忽略，磁盘格式保持
    snake_case 不变。
    """
    session_id: str = Field(default_factory=lambda: str(uuid4()))
    date: str = ""  # 创建日期 YYYY-MM-DD，用于会话目录分桶
    last_updated_at: datetime = Field(default_factory=datetime.now)
    title: str = ""
    # 复盘性描述（会话级摘要），由 maintenance session_summary 定时任务回写
    description: str = ""
    # 会话级模型绑定（config.models 注册表键；空串 = 跟随全局 default）
    model: str = ""
    # 索引的 key，仅 list_sessions 返回时回填，不落盘
    source_id: str = Field(default="", exclude=True)


class SessionSummary(Base):
    """会话复盘（maintenance session_summary 定时任务写入 {session_dir}/summary.json）。

    与压缩记录 CompactRecord（compact.json）相互独立：后者是上下文压缩的
    运行时状态，本模型是会话级的内容复盘。covered_upto 为增量锚点，指向
    被复盘覆盖的最后一条消息 id（load_history 合成视图中的 Message.id）；
    下次运行只把该消息之后的新消息送入 LLM 输入，无新消息则跳过。
    """
    title: str = ""
    description: str = ""
    done: list[str] = Field(default_factory=list)
    went_well: list[str] = Field(default_factory=list)
    went_wrong: list[str] = Field(default_factory=list)
    improvements: list[str] = Field(default_factory=list)
    covered_upto: str = ""
    updated_at: datetime = Field(default_factory=datetime.now)
