"""
Session-specific data models.
"""

from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import Field

from zero.models import Base


class TurnMarker(Base):
    """messages.jsonl 里的一轮事务边界记录（不占 Message 的 sender/body）。

    ``turn_begin`` 在用户消息落盘前写入，``turn_end`` 在本轮最后一条
    消息落盘后写入。二者之间是该轮的全部消息；没有配对的 turn_end
    意味着写入方在轮次中途退出，读取方据此把整轮排除在模型上下文之外。
    """
    kind: Literal["turn_begin", "turn_end"]
    turn_id: str
    # 仅 turn_end 有意义：ok / cancelled / interrupted。读取方只判断
    # 「有没有 end」，status 用于排查而不是决定是否可用。
    status: str = "ok"
    timestamp: datetime = Field(default_factory=datetime.now)


class SessionMeta(Base):
    """会话索引条目（~/.zero/sessions/{channel}/index.json 的 value）。

    索引结构为 dict[source_id, SessionMeta]。旧索引缺少的字段（如 title、
    description）由默认值兜底；未知字段校验时忽略，磁盘格式保持
    snake_case 不变。
    """
    session_id: str = Field(default_factory=lambda: str(uuid4()))
    date: str = ""  # 创建日期 YYYY-MM-DD，用于会话目录分桶
    # 最后活跃时间：不落盘，仅 list_sessions 返回时按 messages.jsonl 的
    # mtime 派生（与 source_id 同属运行时字段）。索引文件里出现该字段会被忽略。
    last_updated_at: datetime = Field(default_factory=datetime.now, exclude=True)
    title: str = ""
    # 复盘性描述（会话级摘要），由 maintenance session_summary 定时任务回写
    description: str = ""
    # 会话级模型绑定（config.models 注册表键；空串 = 跟随全局 default）
    model: str = ""
    # 会话级工作目录（绝对路径；空串 = 未选择）。只有用户入口能写，
    # 模型工具参数不参与，避免通过改参数自行扩大写包络。
    working_dir: str = ""
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
