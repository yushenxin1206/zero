"""
Define data models in the system.
All data classes use Pydantic v2 BaseModel.
"""

from datetime import datetime
from pathlib import Path
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
            raise AttributeError(f"'{name}' not found in {type(self).__name__}. Available: {list(self.model_fields.keys())}") from e


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
    workspace: Path = Path.home() / '.zero' / 'workspace'
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
# LLM request model
# ---------------------------------------------------------------------------

class LLMPayload(Base):
    messages: list[dict[str, Any]] | None = None
    tools: list[dict[str, Any]] | None = None
    llm_config: LLMConfig = Field(default_factory=LLMConfig)

    @property
    def payload_dict(self) -> dict[str, Any]:
        base_data = self.model_dump(exclude={'llm_config'})
        base_data.update(self.llm_config.config_dict or {})
        return base_data



