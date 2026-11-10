# Metadata 契约重构（已完成）

> 状态：已落地，并进一步收紧为「核心层零访问」。
> 本文描述当前代码的契约；`metadata` 已不再是任何核心逻辑的输入或输出。

## 1. 结论

`metadata: dict[str, Any]` 只作为**第三方插件的扩展口袋**保留在
`Message` / `StreamEvent` 上。核心代码（`zero/core/`、`zero/channels/`、
`zero/scheduler/`）一律不读不写。

这条约束不靠 code review，而由 `tests/test_architecture.py` 用 AST 静态
扫描强制：核心层任何 `.metadata` 属性访问或 `metadata=` 关键字实参都会让
测试失败。白名单为空——需要结构化信息时，先把它提升为类型化字段。

`ToolContext.metadata` 同样只留给第三方插件；运行时所需信息（turn_id、
origin、channel_*）都已提升为类型化顶层字段。

## 2. 类型化字段总表

| 原 metadata 约定 | 现在的类型化契约 |
|---|---|
| `message_id` | `Message.channel_message_id` |
| `sender_id` | `Message.channel_sender_id`，工具侧 `ToolContext.channel_sender_id` |
| `chat_id` | `Message.channel_session_id`（渠道侧会话标识：飞书 `chat_id` / CLI `source_id`） |
| `source` + `task_id` | `Message.origin: Origin`（`kind: user/scheduler/subagent` + `task_id`） |
| `turn_id` | `Message.turn_id` / `ToolContext.turn_id` / `StreamEvent.turn_id` |
| `kind == "compact"`（消息） | `Message.compact: bool` |
| `kind`（流事件） | `StreamEvent.kind: StreamEventKind`（`Literal["delta", "reasoning", "tool_call", "done", "error", "approval", "subagent_result"]`） |
| `token_usage` | `StreamEvent.token_usage: TokenUsage \| None` |
| `cancelled` | `StreamEvent.cancelled: bool` |
| 审批事件 content | `StreamEvent.approval_request()` → `ApprovalRequest \| None` |
| `ScheduleTask.metadata`（含 `chat_id` 回退） | 已删除；回投只用 `ScheduleTask.channel_session_id` |
| `ScheduleTask.status` / `schedule_type` | `TaskStatus` / `ScheduleType`（`Literal`） |
| 后台记录 `source` + `task_id` | `BackgroundRecord.origin: Origin` |

其他收紧：

- `Message.sender` / `recipient` 使用 `MessageSender` / `MessageRecipient` Literal，
  拼错角色在构造期即报错；
- `Message.synthetic` 仍用于标记「加载期补写的工具结果缺失占位」，与
  `compact` 是相互独立的语义，不要混用。

## 3. 审批协议

`StreamEvent(kind="approval")` 的 `content` 是 `ApprovalRequest` 的 JSON。
协议的解析入口只有 `StreamEvent.approval_request()`：

- 非审批事件或坏包返回 `None`，调用方自行决定降级行为；
- 消费端不再各自 `json.loads` 猜键名。此前后台渲染读不存在的 `command`
  键，导致「待审批命令」永远为空；该 bug 已修复为读 `ApprovalRequest.summary`，
  并有 `tests/test_cli_background.py` 回归覆盖。

CLI 就地审批（`channels/cli/approval.py`）为了打印校验失败的字段位置，
仍直接调用 `ApprovalRequest.model_validate_json`；这是诊断需要，不构成
第二套协议。

## 4. 数据流（当前）

```
channel
   │  MessageIn(channel_message_id=…, channel_sender_id=…,
   │            channel_session_id=…, origin=…)
   ▼
core/loop.py
   │  ToolContext(turn_id=msg_in.id, origin=msg_in.origin, …)
   ▼
runner / hooks / permission ── 只读类型化字段，无字符串 key
   │
   ▼
StreamEvent / MessageOut ── kind、turn_id、origin、token_usage、cancelled 顶层携带
   │
   ▼
channel ── 按类型化字段路由与渲染；metadata 仅第三方扩展可见
```

## 5. 验收

1. `tests/test_architecture.py` 通过：核心层没有 `.metadata` 访问；
2. 全量 `ruff check .` + `pytest` 通过；
3. 新增/修改的契约都有测试：compact、token_usage/cancelled、approval 解析、
   调度 Literal、渠道能力归类（`ATTENDED_CHANNELS` / `UNATTENDED_CHANNELS`）；
4. 文档（本文与 `docs/hooks.md`）与代码一致。
