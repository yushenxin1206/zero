# Metadata 设计梳理与重构方案

> 状态：已落地（2026-09-17，全部测试通过）
> 范围：`models.py`、`core/`、`channels/`、`scheduler/`、`plugins/shell_guard/`、`tools/scheduler_task.py`
>
> **路径已过期**：文中 `plugins/shell_guard/` 现已删除，等价消费方迁到
> `zero/permission/`。正文保留原样——它记录的是当时的文件与行号，改写会变形
> 成"描述一次从未发生过的迁移"。

## 1. 背景

系统中 `metadata: dict[str, Any]` 目前出现在 5 个核心模型上（`Message`、`StreamEvent`、`ToolContext`、`HookContext`、`ScheduleTask`），共 15 个文件、68 处引用。它是一个无类型的"万能口袋"：任何模块都可以往里塞 key，任何模块都可以读 key，key 的契约散落在各处的字符串字面量里。

实际使用已经暴露出业务逻辑强依赖 metadata 的问题，本文先盘点现状，再给出重构方案。

## 2. 现状盘点：metadata 里到底装了什么

### 2.1 key 全清单（生产方 → 消费方）

| key | 生产方 | 消费方 | 本质 |
|---|---|---|---|
| `message_id` | `channels/cli.py`、`channels/feishu.py` 入队时塞入 `MessageIn.metadata` | `core/loop.py` 读出后放入 `ToolContext.message_id` | 身份字段绕道 |
| `sender_id` | 同上 | `core/loop.py` → `ToolContext.sender_id` | 身份字段绕道 |
| `turn_id` | `core/loop.py` 注入（值就是 `msg_in.id`），写入 `ToolContext.metadata`、`StreamEvent.metadata`、`MessageOut.metadata` | `channels/cli.py` 的 turn 路由（`_route_out` / 后台 turn 认领）；`plugins/shell_guard/handler.py` 审批事件认领 | 路由字段 |
| `source` | cli / feishu / `scheduler/engine.py`（值分别为渠道名或 `"scheduler"`） | `plugins/shell_guard/handler.py` 判 `== "scheduler"` 直接拒绝；`channels/cli.py` 后台 turn 展示 | 来源 / 策略依据 |
| `task_id` | `scheduler/engine.py` | `channels/cli.py` 后台 turn 展示 | 业务溯源 |
| `chat_id` | `channels/feishu.py` | `channels/feishu.py` 回复路由（`message_out.metadata.get("chat_id")`）；`scheduler/engine.py` 注释中说明定时任务结果回投依赖它 | 渠道专有路由 |
| `msg_type` | `channels/feishu.py` | 无人消费 | 死字段 |
| `event_type` | `channels/feishu.py` | 无人消费 | 死字段 |
| `channel` | `scheduler/engine.py` 触发时塞进 `trigger_metadata` | 无人从 metadata 读（`Message.channel` 顶层已有同名字段） | 冗余字段 |

### 2.2 数据流向

```
channel (cli/feishu)
   │  塞 message_id / sender_id / chat_id / source / event_type / msg_type
   ▼
MessageIn.metadata ──────────────┐
   │                             │
   ▼                             ▼
core/loop.py                scheduler/service.py
  读 message_id/sender_id     创建定时任务时把整包
  → ToolContext 顶层          context.metadata 存进
  加 turn_id                  ScheduleTask.metadata
   │                             │
   ▼                             ▼
ToolContext.metadata        scheduler/engine.py
   │                          触发时整包倒回 + source/
   ▼                          task_id/channel → MessageIn
HookContext.metadata
(runner 直接引用 tool_context.metadata)
   │
   ▼
plugins/shell_guard ── 读 source 做安全决策、读 turn_id 做审批路由
StreamEvent.metadata / MessageOut.metadata
   │
   ▼
channels/cli.py ── 读 turn_id 路由、读 source/task_id 展示后台任务
channels/feishu.py ── 读 chat_id 决定回复目标
```

### 2.3 关键代码位置

- `core/loop.py:48-54`：`ToolContext` 构造，`message_id`/`sender_id` 从 `msg_in.metadata` 绕道取，`turn_id` 注入 metadata
- `core/loop.py:78`：每个 `StreamEvent` 复制整包 `msg_in.metadata`
- `core/loop.py:94`：`MessageOut` metadata = 原 metadata + `turn_id`
- `scheduler/engine.py:54-59`：`trigger_metadata = {source, task_id, channel, **task.metadata}`
- `scheduler/service.py:65`：`metadata=dict(metadata or {})` 整包持久化
- `tools/scheduler_task.py:77`：`metadata=dict(context.metadata)` 整包上交
- `plugins/shell_guard/handler.py:53`：`ctx.metadata.get("source") == "scheduler"` 安全决策
- `channels/feishu.py:268`：`message_out.metadata.get("chat_id", "")` 回复路由
- `channels/cli.py:729,860-861`：`turn_id` 路由、`source`/`task_id` 展示

## 3. 问题分析

### 3.1 三类关注点混在一个 dict 里

当前 metadata 里实际混着三种本质完全不同的东西：

1. **基础设施路由**：`turn_id`、`chat_id` —— 框架用来把消息/事件送回正确的地方；
2. **策略依据**：`source`、`task_id` —— 业务用来做决策（shell_guard 的拒绝逻辑、cli 的展示分支）；
3. **渠道私有透传**：`event_type`、`msg_type` —— 只有生产渠道自己懂，且目前根本没人消费。

一个无类型 dict 同时承担三种角色，导致任何一类改动都可能误伤另外两类。

### 3.2 业务逻辑对字符串 key 的强依赖（stringly-typed）

- `shell_guard/handler.py:53` 在**安全决策**里写死 `ctx.metadata.get("source") == "scheduler"`。key 拼错、生产方改名、新增来源类型（如未来加 `heartbeat`）都不会有任何报错，只会静默失效——对安全插件而言这是 fail-open 之外最危险的失败模式。
- `cli.py` 后台 turn 渲染读 `metadata.get("source")` / `metadata.get("task_id")`，同样无类型约束。
- `feishu.py` 回复路由依赖 `metadata.get("chat_id")`，缺失时抛 `ValueError`，属于运行期才能发现的契约。

### 3.3 冗余与双写

- `turn_id`：`StreamEvent` 已有顶层 `turn_id` 字段，metadata 里又复制一份；而它的值就是 `msg_in.id`，本质上根本不需要额外字段传递。
- `channel`：`scheduler/engine.py` 把它塞进 metadata，但 `Message.channel` 顶层已有同名字段，且无人从 metadata 读取。
- `message_id` / `sender_id`：`ToolContext` 顶层就有这两个字段，但赋值方却要从 `msg_in.metadata` 里绕道取——说明它们本就该是 `Message` 的顶层字段，metadata 只是被迫当中转站。

### 3.4 隐式继承链：整包复制、无边界传播

scheduler 链路是"整包进、整包出"：`tools/scheduler_task.py` 把 `context.metadata` 整包存库，`engine.py` 触发时整包倒回 `MessageIn.metadata`。中间任何一环（渠道、插件）往 metadata 塞了什么，都会被定时任务原样携带传播，无法追踪、无法裁剪。

### 3.5 死字段

`event_type`、`msg_type` 只有生产没有消费，是典型的"先塞进去说不定有用"，进一步稀释了 metadata 的信噪比。

## 4. 重构方案

### 4.1 总原则

> **metadata 降级为纯扩展口袋；核心业务契约全部提升为类型化字段。核心代码（loop / runner / hooks / 内置 channel / 内置插件）对 `metadata` 零读取。**

### 4.2 改动一：引入类型化来源模型 `Origin`（替代 `source` / `task_id`）

```python
# models.py
from typing import Literal

class Origin(Base):
    """消息来源：谁触发了这一轮。"""
    kind: Literal["user", "scheduler"] = "user"
    task_id: str | None = None        # kind == "scheduler" 时为 ScheduleTask.id
```

- `Message` 增加顶层字段 `origin: Origin = Field(default_factory=Origin)`；
- `scheduler/engine.py` 触发时构造 `Origin(kind="scheduler", task_id=task.id)`，不再写 `source`/`task_id`/`channel` 三个 metadata key；
- `shell_guard` 改为 `ctx.tool_context.origin.kind == "scheduler"`；
- `cli.py` 后台 turn 判断改为读 `msg.origin.kind` / `msg.origin.task_id`。

收益：来源判断从字符串约定变成类型约束，新增来源类型时所有消费点可被静态检查穷举。

### 4.3 改动二：路由/身份字段归位

1. `Message` 顶层增加 `channel_message_id: str = ""`（渠道侧原始消息 id，如飞书 `om_xxx`）和 `channel_sender_id: str = ""`（渠道侧原始发送者 id，如飞书 `open_id`）；cli/feishu 入队时直接赋顶层字段，`loop.py` 从 `msg_in.channel_message_id` / `msg_in.channel_sender_id` 取，不再绕道 metadata。
   - 命名说明：必须带 `channel_` 前缀。`Message` 自身已有框架生成的 `id`（uuid），若叫 `message_id` 会与框架内部 id 混淆；`sender_id` 也会与 `sender` 角色字段（`user`/`assistant`/`tool`）混淆。`channel_` 前缀明确表达"渠道原样透传、框架不解释其格式"。`ToolContext` 上的同义字段同步改名。
2. `Message` 顶层增加 `channel_session_id: str = ""`（渠道侧会话标识：飞书的 `chat_id`、cli 的会话 id、未来其他 IM 的 conversation_id）。它同时服务两个框架级关注点：
   - **会话解析**：渠道入队时填充，`SessionStore.resolve_session(channel, channel_session_id)` 映射为内部 `session_id`（飞书现在就是这么用 `chat_id` 的，只是把它从 metadata 提为显式契约）；
   - **回复路由**：channel 回复时以它为寻址目标（飞书即 `receive_id`），替代现在 `message_out.metadata.get("chat_id")` 的隐式读法；`MessageOut` 由 `loop.py` 从 `msg_in` 透传该字段。
3. `ToolContext` 增加顶层 `turn_id: str = ""`，由 `loop.py` 直接赋 `msg_in.id`；`shell_guard` 审批事件认领改用 `ctx.tool_context.turn_id`，`HookContext.metadata` 整个字段删除（`tool_context` 引用已足够）。
4. `StreamEvent.metadata`、`MessageOut.metadata` 不再由 `loop.py` 整包复制；`StreamEvent` 已有顶层 `turn_id`，cli 的 turn 路由改走顶层字段 + `origin`。
5. 删除 scheduler 注入的冗余 `channel`，删除 feishu 的死字段 `event_type` / `msg_type`。

### 4.4 改动三：scheduler 回投路由持久化为 `channel_session_id`

定时任务结果回投所需的路由信息，在现有两个渠道下就是"渠道侧会话 id"，因此直接持久化类型化字段，不再整包存 metadata：

```python
# scheduler/models.py
class ScheduleTask(Base):
    ...
    channel_session_id: str = ""   # 渠道侧会话标识，回投寻址用（如飞书 chat_id）
```

- `tools/scheduler_task.py` 存 `context.channel_session_id`（`ToolContext` 同步增加该字段，由 `loop.py` 从 `msg_in` 透传），替代整包 `dict(context.metadata)`；
- `scheduler/engine.py` 触发时把 `channel_session_id` 挂到 `MessageIn.channel_session_id`；
- 回复时 `feishu.py` 改读 `message_out.channel_session_id` 作为 `receive_id`；
- 若未来某渠道回投需要更多私有寻址字段（如飞书的 `receive_id_type` 非默认值场景），再按需引入 `reply_route: dict` 扩展，当前不预先设计。

### 4.5 改动四：metadata 的最终定位

**重构后核心代码写入 metadata 的 key 数量：0。** 现有 9 个 key 全部有明确去向：

| 现有 key | 去向 |
|---|---|
| `message_id` | 提升为 `Message.channel_message_id` |
| `sender_id` | 提升为 `Message.channel_sender_id` |
| `chat_id` | 提升为 `Message.channel_session_id` |
| `source` / `task_id` | 提升为 `Message.origin`（`Origin.kind` / `Origin.task_id`） |
| `turn_id` | 删除——值就是 `msg_in.id`，`StreamEvent` / `ToolContext` 顶层已有 `turn_id` |
| `channel` | 删除——`Message.channel` 顶层已有，metadata 里这份本就无人读取 |
| `event_type` / `msg_type` | 删除——死字段，无消费方 |

`Message.metadata: dict[str, Any]` 字段保留，但：

- 仅作为**第三方插件/未来扩展**的透传口袋；
- 核心代码（`core/`、`channels/`、`scheduler/`、`plugins/` 内置插件）一律不读不写；
- 在 `models.py` 的字段注释中写明这条规约，code review 时把关。

### 4.6 各渠道 `channel_xxx` 字段填充方式

三个字段在各渠道都有现成数据来源，不需要新造概念：

| 字段 | CLI | 飞书 |
|---|---|---|
| `channel_message_id` | 入队时本地 `uuid4()`（现状即如此，CLI 无外部消息系统） | 事件里的 `msg.message_id`（`om_xxx`） |
| `channel_sender_id` | `self._source_id`（`cli-xxxxxxxx`，`_new_session()` 生成并持久化于 binding 文件） | 事件里的 `sender.sender_id.open_id` |
| `channel_session_id` | `self._source_id`（CLI 的会话模型本来就是 `source_id → SessionStore.create_session(CHANNEL, source_id)`，`source_id` 扮演的就是飞书 `chat_id` 的角色） | 事件里的 `msg.chat_id`（`oc_xxx`） |

CLI 填充示例（替换现在 cli.py 的 metadata 构造）：

```python
msg_in = MessageIn(
    session_id=self.session_id,
    channel="cli",
    body=MessageBody(content=text),
    channel_message_id=str(uuid4()),
    channel_sender_id=self._source_id,
    channel_session_id=self._source_id,
)
```

注意区分：`session_id` 是框架内部会话 id（`SessionStore` 生成），`channel_session_id` 是渠道侧会话标识（CLI 的 `source_id`、飞书的 `chat_id`），两者是映射关系而非同一物。CLI 本地渲染路由仍走 `turn_id`；`channel_session_id` 主要为 scheduler 持久化与跨渠道统一契约服务。

### 4.7 目标数据流（重构后）

```
channel
   │  MessageIn(channel_message_id=…, channel_sender_id=…, channel_session_id=…)  ← 顶层字段
   ▼
core/loop.py
   │  ToolContext(turn_id=msg_in.id, origin=msg_in.origin, channel_session_id=…)
   ▼
runner / hooks / plugins ── 全部读类型化字段，无字符串 key
   │
   ▼
StreamEvent / MessageOut ── turn_id、origin、channel_session_id 顶层携带
   │
   ▼
channel ── 按顶层字段路由与渲染；metadata 仅第三方扩展可见
```

## 5. 迁移步骤（兼容式，可分批落地）

| 步骤 | 内容 | 涉及文件 | 验证 |
|---|---|---|---|
| 1 | `models.py` 加 `Origin`、顶层字段（`channel_message_id`/`channel_sender_id`/`channel_session_id`/`ToolContext.turn_id`）；生产方**双写**（新字段 + 旧 metadata key） | `models.py`、`core/loop.py`、`channels/cli.py`、`channels/feishu.py`、`scheduler/engine.py` | 现有测试全绿 |
| 2 | 迁移消费方：shell_guard → cli → feishu → runner；删除 `HookContext.metadata` | `plugins/shell_guard/handler.py`、`approval.py`、`channels/cli.py`、`channels/feishu.py`、`core/runner.py`、`core/hooks.py` | `pytest tests/test_shell_guard.py` |
| 3 | `ScheduleTask` 加 `channel_session_id`，scheduler 链路从整包 metadata 切换为该字段 | `scheduler/models.py`、`service.py`、`engine.py`、`tools/scheduler_task.py` | 手动创建一次性定时任务验证回投 |
| 4 | 删除 metadata 双写与死字段（`source`/`task_id`/`channel`/`event_type`/`msg_type`/`turn_id` 的 metadata 形态） | 同步骤 1 文件 | 全量测试 + grep 确认核心代码无 `metadata.get` |
| 5 | 更新测试夹具与文档 | `tests/test_shell_guard.py`、`tests/smoke_cli_async_render.py`、本文档 | CI |

注意点：

- `scheduler` 的持久化数据（`ScheduleTask`）若已有线上数据，步骤 3 需要一次性迁移脚本或双读兼容（先读 `channel_session_id`，缺失时回落旧 `metadata.chat_id`）。
- `SessionStore` 持久化的历史 `Message` 带旧 metadata，步骤 2 期间读取侧需容忍两套形态（pydantic 默认值可天然兼容）。

## 6. 验收标准

1. 核心代码（`core/`、`channels/`、`scheduler/`、`plugins/`）中 `grep -n "metadata.get\|metadata\["` 结果为空；
2. 来源/路由相关的所有判断均为类型化字段访问，无字符串 key 字面量；
3. `metadata` 字段在 `models.py` 中带"仅扩展用"规约注释；
4. 全部现有测试通过，新增 `Origin` / `channel_session_id` 回投链路的单测。
