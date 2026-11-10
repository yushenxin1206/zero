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

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel


class Base(BaseModel):
    """Base model with camelCase alias generation."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


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


# 消息来源：类型与取值常量。字符串字面量只在下面出现一次，构造与比较
# 一律引用常量——仓库没有 mypy 常驻，拼错名字会立刻 NameError，比注解
# 更能挡住运行期"静默比成 false 走错分支"。
OriginKind = Literal["user", "scheduler", "subagent"]
ORIGIN_USER: OriginKind = "user"
ORIGIN_SCHEDULER: OriginKind = "scheduler"
ORIGIN_SUBAGENT: OriginKind = "subagent"


class Origin(Base):
    """消息来源：谁触发了这一轮。"""
    kind: OriginKind = ORIGIN_USER
    # kind == ORIGIN_SCHEDULER 时为 ScheduleTask.id；kind == ORIGIN_SUBAGENT
    # 时为 SubagentManager 分配的子任务 id（回投消息据此标识来源）
    task_id: str | None = None


# 内置通道标识。channel 字段保持开放 str（插件/第三方通道可自由接入），
# 这里只固定内置三者的名字，供构造与比较点引用，不做封闭 Literal。
CHANNEL_CLI = "cli"
CHANNEL_WEB = "web"
CHANNEL_FEISHU = "feishu"

MessageSender = Literal["user", "assistant", "tool"]
MessageRecipient = Literal["assistant", "user"]
# 一轮执行结束时 AgentLoop 对外回报的状态；跨 core / scheduler 使用，
# 放在依赖叶子避免调度器反向 import core。
TurnStatus = Literal["completed", "error", "cancelled"]


class Message(Base):
    id: str = Field(default_factory=lambda: str(uuid4()))
    # 外键，必须显式给：默认生成 uuid 会让漏传 session_id 的消息静默落进
    # 一个随机新会话，而不是在构造时报错。
    session_id: str
    channel: str = ""
    sender: MessageSender
    recipient: MessageRecipient
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
    # 加载期合成的上下文压缩消息。仅由 SessionStore.apply_compact 置 True，
    # 渲染方据此与真实用户发言区分样式；不落盘。
    compact: bool = False
    # 加载期补写的「工具结果缺失」占位。真实消息恒为 False；读取方据此
    # 区分占位与真实结果，不靠正文文案匹配。
    synthetic: bool = False
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
    sender: MessageSender = "user"
    recipient: MessageRecipient = "assistant"


class MessageOut(Message):
    sender: MessageSender = "assistant"
    recipient: MessageRecipient = "user"


class MessageTool(Message):
    """Tool execution result message, produced by agent during tool calls."""
    sender: MessageSender = "tool"
    recipient: MessageRecipient = "assistant"


class StopRequest(Base):
    """用户终止指定 turn 的旁路信号（bus.stop_queue）。

    不占用 in_queue（AgentLoop 串行消费，当前 turn 结束前轮不到下一条），
    由 AgentLoop 后台协程实时消费，仅当精确匹配当前活跃 turn 时置位其
    stop_event。channel 无关：CLI/飞书等任何渠道均可发。
    """
    channel: str
    session_id: str
    turn_id: str = ""


class CompactRecord(Base):
    """会话历史压缩记录（{session_dir}/compact.json）。

    covered_upto 指向被压缩进记录的最后一条原始消息 id（messages.jsonl 中
    某条 Message.id）；load_history 据此定位尾部起点，合成 [压缩消息] + 尾部。
    content 已包裹 <compact>...</compact> 标签，由压缩模型生成。
    """
    content: str
    covered_upto: str
    created_at: datetime = Field(default_factory=datetime.now)


StreamEventKind = Literal[
    "delta", "reasoning", "tool_call", "done", "error", "approval",
    "subagent_result",
]


class StreamEvent(Base):
    """Incremental agent output event, published on the bus stream queue.

    Carries the assistant's reply while it is being generated so interactive
    channels (CLI) can render it in real time. Final assembled messages are
    still delivered via MessageOut as usual.

    kind:
      - "delta":     a chunk of assistant text, appended to previous deltas
      - "reasoning": a chunk of the model's reasoning/thinking text (reasoner
                     models only). Display-only: it is never part of the final
                     reply and never persisted to session history
      - "tool_call": a tool call started (content = "name(args)")
      - "done":      the reply for this turn is complete (content = full text)
      - "error":     the turn failed (content = error message)
      - "approval":  in-place approval request (content = JSON 序列化的
                     ApprovalRequest), emitted by the requesting component
                     itself with routing fields stamped
      - "subagent_result": 子 agent 完成后的结果原文（content = 结果文本）。
                     由 SubagentManager 直接发出，供 channel 原样展示，
                     不经主对话转述（回投另有独立消息，让主对话据结果继续）

    origin 由 AgentLoop 从触发消息的 origin 复制，让 channel 端能识别
    后台 turn 的来源（如定时任务结果）。
    """
    channel: str = ""
    session_id: str = ""
    turn_id: str = ""  # id of the MessageIn this event belongs to
    kind: StreamEventKind = "delta"
    content: str = ""
    origin: Origin = Field(default_factory=Origin)
    # 仅 done 事件携带：本轮模型调用的 token 用量（厂商未上报则 None）
    token_usage: TokenUsage | None = None
    # 仅 done 事件携带：本轮因用户终止而提前收尾（content 为已生成片段）
    cancelled: bool = False
    # 仅第三方插件扩展使用；核心代码不读不写
    metadata: dict[str, Any] = Field(default_factory=dict)

    def approval_request(self) -> "ApprovalRequest | None":
        """kind == "approval" 时解析 content 承载的审批协议。

        非审批事件或坏包返回 None。协议形状只在这里定义一次，消费端不再
        各自 ``json.loads`` 猜键名。
        """
        if self.kind != "approval":
            return None
        try:
            return ApprovalRequest.model_validate_json(self.content)
        except ValidationError:
            return None


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
    zhipu: ProviderConfig = Field(default_factory=ProviderConfig)


class LLMCallOptions(Base):
    """单次 LLM 调用的选项：请求里除内容（messages / tools）之外的旋钮。

    与 providers.base.LLMPayload 成正交的一对：Payload 装内容，本类装
    怎么调（模型选择、采样、输出格式、流式等）。字段与 OpenAI 的
    ChatCompletionCreateParams 同名同义，故可直接透传给 SDK。
    """
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
    def options_dict(self) -> dict[str, Any]:
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
    # 0 = 未声明。窗口决定压缩触发点，猜大则永不压缩直到超窗报错、猜小则
    # 多花压缩成本，所以不给"看起来能跑"的默认值，由 resolve_runtime 拒绝。
    context_window_tokens: int = 0


class AgentRuntimeSpec(Base):
    """一个 agent runtime 的声明形态（config.json "agentRuntimes" 的 value）。

    键为任务名（default 主对话 / compact 上下文压缩 / session_summary 会话
    复盘 / memory 记忆沉淀，与 scheduler 的定时任务无关），值描述"该任务跑
    起来是什么样"：策略字段 + 该任务自己的 LLMCallOptions。策略字段与调用
    选项共用同一条字段级优先级 task 显式 > default 显式 > 数据模型默认，
    由 Config.resolve_runtime 结算（见该方法）。

    llm_call_options.model 为 models 注册表键（模型绑定的唯一入口，空 =
    未绑定，未绑定时由解析方决定回退：主对话报错、派生任务跟随 default）。
    默认空对象即"未覆盖任何调用选项、整条继承 default"；是否继承由
    model_fields_set 表达（磁盘未写该键 = 继承基座），显式写 llmCallOptions
    则逐子键与 default 合并。fields_set 来自磁盘文件的键集合，即手工维护的
    精简配置是显式性的唯一来源。

    策略字段（max_iterations）对派生任务会一并继承但不被消费——只有主对话
    做上下文压缩。压缩功能本身的策略（开关/触发比例/尾部比例/输出上限）
    已迁至顶层 CompactConfig（与 maintenance 同级），见 config.compact；
    compact/session_summary/memory 的模型/temperature 仍走本任务的
    llm_call_options，输出上限分别由 CompactConfig / MaintenanceConfig 决定。

    extra='forbid' 让未知键（如改名前的 llmConfig、手滑拼错的键）在加载时
    立即报错——默认的 extra='ignore' 会把它丢掉，最终表现为"未绑定模型"
    这类指向错误方向的报错。
    """
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra='forbid')

    max_iterations: int = 50
    # 单次 LLM 调用为 reasoning 思考预留的 completion 预算余量（思考与正文共享预算）
    reasoning_token_headroom: int = 4096
    llm_call_options: LLMCallOptions = Field(default_factory=LLMCallOptions)


class AgentRuntimeSpecs(Base):
    """agentRuntimes 的容器（config.json "agentRuntimes" 的值）。

    只收录已知任务名：default 主对话 / compact 上下文压缩 /
    session_summary 会话复盘 / memory 记忆沉淀 / subagent 子 agent 单次
    任务。extra='forbid' 让未知任务名在加载时立即报错，而不是被静默忽略
    后悄悄回退。
    """
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra='forbid')

    default: AgentRuntimeSpec = Field(default_factory=AgentRuntimeSpec)
    compact: AgentRuntimeSpec | None = None
    session_summary: AgentRuntimeSpec | None = None
    memory: AgentRuntimeSpec | None = None
    # 子 agent 单次任务：可绑定与主对话不同的模型（如更便宜的模型跑并行子任务）
    subagent: AgentRuntimeSpec | None = None

    def get(self, name: str) -> AgentRuntimeSpec | None:
        """按任务名取配置；未知任务名返回 None（视为未绑定）。"""
        return getattr(self, name, None)


class LLMRuntime(Base):
    """本次调用用到的模型接入事实：ModelSpec（注册表声明）的物化版。

    由 Config.resolve_runtime() 结算：provider 已按 ModelSpec.provider 键
    在 ProviderConfigs 中换成 ProviderConfig 对象，context_window_tokens
    取自注册表条目。与 LLMCallOptions 的分工——本类回答"打到哪个端点、
    窗口多大"（查表得来，用户不写），LLMCallOptions 回答"这次怎么调"
    （用户写）。
    """
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    # 由 resolve_runtime 显式传入；不设默认值，避免与 ModelSpec 的同名
    # 默认值形成第二份真相（两处各写一个窗口，迟早漂移）。
    context_window_tokens: int


class AgentRuntimeConfig(AgentRuntimeSpec):
    """AgentRuntimeSpec 的解析产物：声明 + 物化的模型事实。

    由 Config.resolve_runtime() 构造：策略字段与 llm_call_options 走同一
    条 task 显式 > default 显式 > 数据模型默认 优先级链合并而来；
    llm_runtime 由合并出的 model（LLMCallOptions.model）查 models 注册表
    结算。继承 AgentRuntimeSpec 使字段单点定义，且 spec 的全部字段即本类
    合法构造 kwargs（llm_call_options 类型与 Spec 一致，同为非空）。

    压缩记录的输出上限与压缩策略由顶层 CompactConfig（config.compact）
    决定，不在本类承载。
    """
    # 必填：解析产物缺少接入事实（端点/窗口）就是无效对象，不该能凭空构造。
    llm_runtime: LLMRuntime


class FeishuConfig(Base):
    """Feishu bot configuration for WebSocket long connection."""
    app_id: str = ''
    app_secret: str = ''


class WebConfig(Base):
    """Web 通道配置：本地 WebSocket 服务 + pywebview 桌面窗口。

    四个字段性质不同，因此都留在配置里，但改动动机不一样：

    - ``width`` / ``height``：窗口尺寸是使用者偏好，手改是正常用法，
      这里也是全局唯一的默认值来源（shell 不再重复一套默认值）。
    - ``port``：运行期可用性事实。``0`` 表示由系统分配空闲端口，窗口通过
      js_api 的 bootstrap() 取得实际地址，避免固定端口被占用时整体启动失败；
      只有需要外部客户端固定连接时才值得手改成具体端口。
    - ``host``：安全边界。默认回环地址；改成 ``0.0.0.0`` 等于把能执行
      shell 的本地服务暴露到局域网，属于刻意承担风险的操作。
    """
    host: str = '127.0.0.1'
    port: int = 0
    width: int = 1120
    height: int = 760


class ChannelConfigs(Base):
    """Channel configurations container for extensible channel support."""
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)
    web: WebConfig = Field(default_factory=WebConfig)


class CompactConfig(Base):
    """上下文压缩功能配置（config.json 顶层 "compact"，与 maintenance 同级）。

    压缩是主对话的运行时行为，但作为独立功能统一在此配置：开关、触发
    比例、尾部保留比例、压缩记录输出上限。compact 任务的模型/temperature
    等调用选项仍走 agentRuntimes.compact 条目（model 跟随会话绑定）；
    reasoning 余量取各自 runtime 的 reasoning_token_headroom。
    """
    enabled: bool = True
    # 触发阈值 min(窗口×比例, 窗口-max_tokens-reasoning_token_headroom)
    trigger_ratio: float = 0.75
    # 保留在 payload 中的尾部预算（软上限，按主对话窗口的比例）
    tail_ratio: float = 0.25
    # 压缩记录输出上限（不再取 compact 任务 llm_call_options.max_tokens）
    context_compact_max_tokens: int = 2000


class MaintenanceConfig(Base):
    """后台维护任务：会话复盘（session_summary）与长期记忆沉淀（memory）。

    两个任务独立开关、独立 cron；由 maintenance.MaintenanceEngine 在进程内
    调度，不经过 Scheduler/AgentLoop。各任务的模型/temperature/reasoning
    余量仍走 agentRuntimes 对应条目，本区块只放"开关/频率/输出上限"策略。
    """
    session_summary_enabled: bool = True
    session_summary_cron: str = "0 23 * * *"  # 会话复盘：每天 23:00 复盘 24h 内活跃会话
    session_summary_max_tokens: int = 2000   # 复盘正文（六字段 JSON）的输出上限
    memory_enabled: bool = True
    memory_cron: str = "30 23 * * *"  # 记忆沉淀：每天 23:30 从复盘提炼长期记忆
    memory_max_tokens: int = 8000     # 记忆文件重写输出上限（约束 ~100 行 Markdown）


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


# ---------------------------------------------------------------------------
# Plan models
# ---------------------------------------------------------------------------

PlanStatus = Literal[
    "draft", "reviewing", "approved", "executing", "done", "aborted"]
StepStatus = Literal["pending", "in_progress", "done", "blocked", "skipped"]


class PlanStep(Base):
    """计划中的单个步骤。index 从 1 起，note 存结果摘要或阻塞原因。"""
    index: int
    text: str
    status: StepStatus = "pending"
    note: str = ""


class PlanChange(Base):
    """变更记录条目（append-only）：谁/为什么改了什么。"""
    at: datetime
    note: str


class PlanState(Base):
    """一个会话的活跃计划状态——plan skill 的唯一事实源。

    由 plan skill 的内置脚本（zero/skills/plan/scripts/plan.py）读写，
    序列化为会话数据目录下的 plan.json（UI/消费方直接读该文件，不做
    任何 Markdown 解析）；同目录的 plan.md 是它的人类可读视图，只由
    本模型渲染生成，不接受手工编辑。
    """
    task: str = ""
    status: PlanStatus = "draft"
    goal: str = ""          # 目标 + 验收标准
    findings: str = ""      # 调研结论：现状 / 约束 / 已排除方案
    steps: list[PlanStep] = Field(default_factory=list)
    changes: list[PlanChange] = Field(default_factory=list)
    updated_at: datetime
