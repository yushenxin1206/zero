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
from typing import Any
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


class MessageBody(Base):
    content: str | None = None  # None when assistant only invokes tools
    attachments: list | None = None
    tool_calls: list[ToolCall] | None = None  # only on assistant messages
    tool_call_id: str | None = None  # only on tool messages, links to ToolCall.id


class Message(Base):
    id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str = Field(default_factory=lambda: str(uuid4()))
    channel: str = ""
    sender: str
    recipient: str
    subject: str | None = None
    body: MessageBody
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
      - "approval":  in-place approval request from a hook plugin
                     (content = JSON {"req_id", "command", "reason",
                     "deadline"} — deadline 为 unix 时间戳，CLI 按它做
                     单键倒计时，到时打印超时拦截提示),
                     emitted by the plugin itself with routing fields stamped

    metadata 由 AgentLoop 从触发消息的 metadata 复制（含 scheduler 注入的
    source / task_id 等），让 channel 端能识别后台 turn 的来源。
    """
    channel: str = ""
    session_id: str = ""
    turn_id: str = ""  # id of the MessageIn this event belongs to
    kind: str = "delta"
    content: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


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

    跨渠道通用字段放在顶层；渠道专有字段（如飞书的 chat_id）放在 metadata。
    """
    channel: str = ""
    session_id: str = ""
    message_id: str = ""
    sender_id: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
