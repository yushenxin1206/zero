"""
Cross-module contracts: message protocol, tool context, and configuration schema.

All data classes use Pydantic v2 BaseModel. This module is a dependency-free
leaf: it imports nothing from other project modules, so any module may import
it without creating an import cycle.

Module-specific models live in their own modules:
- scheduler.models.ScheduleTask
- session.models.SessionMeta
- providers.base.LLMPayload
"""

from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class Base(BaseModel):
    """Base model with camelCase alias generation."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    def get(self, name: str) -> Any:
        """Safely retrieve a field by name with error handling."""
        try:
            return getattr(self, name)
        except AttributeError as e:
            raise AttributeError(
                f"'{name}' not found in {type(self).__name__}. Available: {list(self.model_fields.keys())}") from e


# ---------------------------------------------------------------------------
# Message models
# ---------------------------------------------------------------------------

class ToolCallFunction(Base):
    """Function part of a tool call, aligned with OpenAI protocol."""
    name: str
    arguments: str  # JSON string, as returned by LLM


class ToolCall(Base):
    """A single tool call from LLM, aligned with OpenAI protocol."""
    id: str
    type: str = "function"
    function: ToolCallFunction


class TokenUsage(Base):
    """单次 LLM 调用的 token 用量，字段命名对齐 OpenAI 协议。

    厂商不返回 usage 时整体为 None；部分字段（如 cached_tokens）只有
    部分厂商返回，故可空。
    """
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None

    @classmethod
    def from_openai(cls, data: dict[str, Any]) -> "TokenUsage":
        """Parse an OpenAI-style usage dict, flattening *_details sub-objects."""
        prompt_details = data.get("prompt_tokens_details") or {}
        completion_details = data.get("completion_tokens_details") or {}
        return cls(
            prompt_tokens=data.get("prompt_tokens", 0),
            completion_tokens=data.get("completion_tokens", 0),
            total_tokens=data.get("total_tokens", 0),
            cached_tokens=prompt_details.get("cached_tokens"),
            reasoning_tokens=completion_details.get("reasoning_tokens"),
        )


class MessageBody(Base):
    content: str | None = None  # None when assistant only invokes tools
    attachments: list | None = None
    tool_calls: list[ToolCall] | None = None  # only on assistant messages
    tool_call_id: str | None = None  # only on tool messages, links to ToolCall.id
    token_usage: TokenUsage | None = None  # only on assistant messages; None = 厂商未上报


class Origin(Base):
    """消息来源：谁触发了这一轮。"""
    kind: Literal["user", "scheduler"] = "user"
    task_id: str | None = None  # kind == "scheduler" 时为 ScheduleTask.id


class Message(Base):
    id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str = Field(default_factory=lambda: str(uuid4()))
    channel: str = ""
    sender: str
    recipient: str
    subject: str | None = None
    body: MessageBody
    # 触发本轮的消息来源（用户输入 / 定时任务）
    origin: Origin = Field(default_factory=Origin)
    # 触发本轮的 MessageIn.id（仅 MessageOut 使用，channel 端据此把最终
    # 消息精确匹配到触发它的那一轮）
    turn_id: str = ""
    # 渠道原样透传、框架不解释其格式的字段（channel_ 前缀规约）：
    channel_message_id: str = ""  # 渠道侧原始消息 id（飞书 om_xxx；cli 本地 uuid）
    channel_sender_id: str = ""  # 渠道侧原始发送者 id（飞书 open_id；cli source_id）
    channel_session_id: str = ""  # 渠道侧会话标识（飞书 chat_id；cli source_id）| 会话解析与回复路由（含定时任务回投）使用
    # 仅第三方插件/未来扩展使用的透传口袋；核心代码（core/ channels/
    # scheduler/ 内置 plugins）一律不读不写
    metadata: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.now)


class MessageIn(Message):
    sender: str = "user"
    recipient: str = "assistant"


class MessageOut(Message):
    sender: str = "assistant"
    recipient: str = "user"


class MessageTool(Message):
    """Tool execution result message, produced by agent during tool calls."""
    sender: str = "tool"
    recipient: str = "assistant"


class StreamEvent(Base):
    """Incremental agent output event, published on the bus stream queue.

    Carries the assistant's reply while it is being generated so interactive
    channels (CLI) can render it in real time. Final assembled messages are
    still delivered via MessageOut as usual.

    kind:
      - "delta":     a chunk of assistant text, appended to previous deltas
      - "tool_call": a tool call started (content = "name(args)")
      - "done":      the reply for this turn is complete (content = full text)
      - "error":     the turn failed (content = error message)
      - "approval":  in-place approval request (content = JSON 序列化的
                     ApprovalRequest), emitted by the requesting component
                     itself with routing fields stamped

    origin 由 AgentLoop 从触发消息的 origin 复制，让 channel 端能识别
    后台 turn 的来源（如定时任务结果）。
    """
    channel: str = ""
    session_id: str = ""
    turn_id: str = ""  # id of the MessageIn this event belongs to
    kind: str = "delta"
    content: str = ""
    origin: Origin = Field(default_factory=Origin)
    # 仅第三方插件扩展使用；核心代码不读不写
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Approval protocol
# ---------------------------------------------------------------------------

class ApprovalOption(Base):
    """单键审批选项：key 为作答字符，label 为动作名，description 为含义。"""
    key: str
    label: str
    description: str = ""


class ApprovalRequest(Base):
    """通用就地审批请求——StreamEvent(kind="approval") 的 content 协议。

    任何需要人工确认的组件（hook 插件等）构造该模型、JSON 序列化后入
    stream 总线；channel 端只按本模型渲染通用审批面板，答案经
    bus.approvals 回填，全程不认识审批的业务来源。
    """
    req_id: str
    title: str  # 面板标题
    summary: str  # 主体加粗行（如待审批的命令）
    detail: str = ""  # 主体次要说明（dim 渲染）
    options: list[ApprovalOption]  # 可作答的键位集合
    timeout_hint: str = ""  # 超时按拒绝处理时展示的引导文案（业务方提供）
    deadline: float = 0.0  # unix 时间戳，channel 与后端共用的权威超时点


# ---------------------------------------------------------------------------
# Configuration models
# ---------------------------------------------------------------------------

class ProviderConfig(Base):
    api_base: str = ''
    api_key: str = ''


class ProviderConfigs(Base):
    dashscope: ProviderConfig = Field(default_factory=ProviderConfig)
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)


class LLMConfig(Base):
    model: str = ''
    tool_choice: dict | str | None = None
    response_format: dict[str, Any] = {'type': 'text'}
    stop: list[str] | None = None
    stream: bool | None = None
    stream_options: dict[str, Any] | None = None
    temperature: float | int = 0.1
    max_tokens: int = 8192
    reasoning_effort: str | None = None
    extra_body: dict[str, Any] | None = None

    @property
    def config_dict(self) -> dict[str, Any]:
        base_data = self.model_dump(exclude={'extra_body'})
        base_data.update(self.extra_body or {})
        return base_data


class AgentRuntimeConfig(Base):
    provider: str = ''
    llm_config: LLMConfig = Field(default_factory=LLMConfig)
    context_window_tokens: int = 65536
    max_iterations: int = 50


class FeishuConfig(Base):
    """Feishu bot configuration for WebSocket long connection."""
    app_id: str = ''
    app_secret: str = ''


class ChannelConfigs(Base):
    """Channel configurations container for extensible channel support."""
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)


# ---------------------------------------------------------------------------
# Tool context model
# ---------------------------------------------------------------------------

class ToolContext(Base):
    """Runtime context auto-injected into every tool call by the runner.

    全部为类型化顶层字段；metadata 仅第三方插件扩展使用，核心代码不读不写。
    """
    channel: str = ""
    session_id: str = ""
    turn_id: str = ""  # 触发本轮的 MessageIn.id，hooks/插件据此挂载交互事件
    origin: Origin = Field(default_factory=Origin)  # 本轮来源（user / scheduler）
    channel_message_id: str = ""  # 渠道侧原始消息 id
    channel_sender_id: str = ""  # 渠道侧原始发送者 id
    channel_session_id: str = ""  # 渠道侧会话标识，定时任务回投寻址用
    metadata: dict[str, Any] = Field(default_factory=dict)
