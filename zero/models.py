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
from typing import Any, Literal, Mapping
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

    def to_openai(self) -> dict[str, Any] | None:
        """转换为 OpenAI 协议消息 dict；未知 sender 无法映射时返回 None。"""
        if self.sender == "user":
            return {"role": "user", "content": self.body.content}
        if self.sender == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": self.body.content}
            if self.body.tool_calls:
                msg["tool_calls"] = [tc.model_dump() for tc in self.body.tool_calls]
            return msg
        if self.sender == "tool":
            return {
                "role": "tool",
                "tool_call_id": self.body.tool_call_id,
                "content": self.body.content,
            }
        return None


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


class CompactRecord(Base):
    """会话历史压缩记录（{session_dir}/compact.json）。

    covered_upto 指向被压缩进记录的最后一条原始消息 id（messages.jsonl 中
    某条 Message.id）；load_history 据此定位尾部起点，合成 [压缩消息] + 尾部。
    content 已包裹 <compact>...</compact> 标签，由压缩模型生成。
    """
    content: str
    covered_upto: str
    created_at: datetime = Field(default_factory=datetime.now)


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


class ModelSpec(Base):
    """模型注册表条目（config.json "models" 的 value）。

    键为 API 模型名（如 "deepseek-v4-flash"），值声明该模型的接入事实：
    provider 指向 providers 配置的键，context_window_tokens 供主对话的
    上下文压缩阈值计算使用。
    """
    provider: str = ''
    context_window_tokens: int = 65536


class TaskConfig(Base):
    """任务级模型绑定（config.json "tasks" 的 value）。

    这里的"任务"指 LLM 调用场景（default 主对话 / compact 上下文压缩 /
    session_summary 会话复盘 / memory 记忆沉淀），与 scheduler 的定时
    任务无关。model 为 models 注册表键；未设置时由解析方决定回退
    （主对话报错、派生任务跟随主对话）。llm_config 是 overlay：仅覆盖
    显式设置的字段，其余继承基座（绑定模型的默认值或主对话配置）。
    """
    model: str | None = None
    llm_config: LLMConfig | None = None


class AgentRuntimeSpec(Base):
    """agentRuntime 的声明形态（config.json 里用户手写的形状）。

    只声明策略，不重复模型事实：模型与调用参数由 tasks/models 按任务
    解析（见 resolve_runtime）。extra='forbid' 让遗留字段（如旧版内联
    的 provider/llmConfig）在加载时立即报错，而不是被静默忽略后悄悄
    回退到默认值。
    """
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra='forbid')

    task: str = 'default'
    max_iterations: int = 50
    # 会话上下文自动压缩：历史超出阈值时，把前缀压缩成摘要后落盘
    context_compress_enabled: bool = True
    context_compress_trigger_ratio: float = 0.75  # 触发阈值 min(窗口×比例, 窗口-max_tokens-4096)
    context_compress_tail_ratio: float = 0.25  # 保留在 payload 中的尾部预算（软上限）


class AgentRuntimeConfig(AgentRuntimeSpec):
    """agentRuntime 的解析产物：spec 声明 + 物化的模型事实。

    由 resolve_runtime() 构造：provider/llm_config/context_window_tokens
    来自解析出的 ModelSpec 与 TaskConfig，context_compress_output_tokens
    由 tasks.compact.llmConfig.maxTokens 回填。继承 AgentRuntimeSpec 使
    策略字段单点定义，且 spec 的全部字段即本类合法构造 kwargs。
    """
    provider: str = ''
    llm_config: LLMConfig = Field(default_factory=LLMConfig)
    context_window_tokens: int = 65536
    context_compress_output_tokens: int = 2000  # 压缩模型输出的 compact 记录上限


class ResolvedLLM(Base):
    """派生任务（compact/session_summary/memory）的解析产物。

    与 AgentRuntimeConfig 的关系：后者是主对话专属（额外携带 spec 策略
    字段），本模型只含派生任务需要的模型事实——provider 键与合成后的
    调用参数。
    """
    provider: str = ''
    llm_config: LLMConfig = Field(default_factory=LLMConfig)


# 压缩输出上限的默认值：tasks.compact 未显式配置 maxTokens 时的兜底
DEFAULT_COMPACT_OUTPUT_TOKENS = 2000


def _task_overlay(task_cfg: TaskConfig | None, base: LLMConfig) -> LLMConfig:
    """把任务级 llm_config overlay 叠加到基座上（仅显式设置的字段）。"""
    if task_cfg is None or task_cfg.llm_config is None:
        return base
    return base.model_copy(
        update=task_cfg.llm_config.model_dump(exclude_unset=True))


def resolve_runtime(
        spec: AgentRuntimeSpec,
        tasks: Mapping[str, TaskConfig],
        models: Mapping[str, ModelSpec],
        session_model: str = '',
) -> AgentRuntimeConfig:
    """解析主对话运行时配置（AgentLoop 每 turn 调用一次）。

    模型绑定优先级：session_model（会话内 /model 切换的覆盖）>
    tasks[spec.task].model；两者皆空视为配置缺失，立即报错（fail-fast，
    防止静默回退默认模型后用错误配置跑对话）。llm_config 以绑定模型的
    LLMConfig 默认值为基座叠加任务级 overlay。context_window_tokens
    取自 ModelSpec；context_compress_output_tokens 由 tasks.compact 的
    llmConfig.maxTokens 显式值回填（默认 2000）。
    """
    task_cfg = tasks.get(spec.task)
    model_name = session_model or (task_cfg.model if task_cfg else None)
    if not model_name:
        raise ValueError(
            f"任务 {spec.task!r} 未绑定模型：请在 config.json 的 "
            f"tasks.{spec.task}.model 指定 models 注册表中的键")
    ms = models.get(model_name)
    if ms is None:
        raise ValueError(
            f"模型 {model_name!r} 不在 models 注册表中，已知键: {sorted(models)}")
    return AgentRuntimeConfig(
        **spec.model_dump(),
        provider=ms.provider,
        llm_config=_task_overlay(task_cfg, LLMConfig(model=model_name)),
        context_window_tokens=ms.context_window_tokens,
        context_compress_output_tokens=_compact_output_tokens(tasks),
    )


def resolve_task_llm(
        task: str,
        tasks: Mapping[str, TaskConfig],
        models: Mapping[str, ModelSpec],
        fallback: ResolvedLLM,
) -> ResolvedLLM:
    """解析派生任务的模型与调用参数（compact/session_summary/memory）。

    模型绑定：tasks[task].model 未设置时跟随 fallback（主对话有效配置），
    未显式绑定模型的任务默认与会话同模型，避免上下文口径漂移。
    fallback 通常传 resolve_runtime() 的产物（AgentRuntimeConfig 是
    ResolvedLLM 的结构超集）。任务绑定模型时 llm_config 以该模型的
    LLMConfig 默认值为基座；overlay 语义与 resolve_runtime 一致。
    """
    task_cfg = tasks.get(task)
    if task_cfg is None or task_cfg.model is None:
        provider, base = fallback.provider, fallback.llm_config.model_copy()
    else:
        ms = models.get(task_cfg.model)
        if ms is None:
            raise ValueError(
                f"tasks.{task}.model {task_cfg.model!r} 不在 models 注册表中，"
                f"已知键: {sorted(models)}")
        provider, base = ms.provider, LLMConfig(model=task_cfg.model)
    return ResolvedLLM(provider=provider,
                       llm_config=_task_overlay(task_cfg, base))


def _compact_output_tokens(tasks: Mapping[str, TaskConfig]) -> int:
    """压缩输出上限：tasks.compact.llmConfig 显式设置的 maxTokens。

    只认显式设置（model_fields_set）——任务配了 llmConfig 但没写
    maxTokens 时不应把 LLMConfig 的字段默认值 8192 误当成输出上限。
    """
    cc = tasks.get('compact')
    if (cc is not None and cc.llm_config is not None
            and 'max_tokens' in cc.llm_config.model_fields_set):
        return cc.llm_config.max_tokens
    return DEFAULT_COMPACT_OUTPUT_TOKENS


class FeishuConfig(Base):
    """Feishu bot configuration for WebSocket long connection."""
    app_id: str = ''
    app_secret: str = ''


class ChannelConfigs(Base):
    """Channel configurations container for extensible channel support."""
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)


class MaintenanceConfig(Base):
    """后台维护任务：会话复盘（session_summary）与长期记忆沉淀（memory）。

    两个任务独立开关、独立 cron；由 maintenance.MaintenanceEngine 在进程内
    调度，不经过 Scheduler/AgentLoop。
    """
    session_summary_enabled: bool = True
    session_summary_cron: str = "0 23 * * *"  # 会话复盘：每天 23:00 复盘 24h 内活跃会话
    memory_enabled: bool = True
    memory_cron: str = "30 23 * * *"  # 记忆沉淀：每天 23:30 从复盘提炼长期记忆


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
