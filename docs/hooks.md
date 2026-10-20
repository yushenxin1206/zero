# Hooks 模块完整设计方案

> 承接 `shell_permission.md`（v4）的结论：核心做一次通用、零策略的 hooks
> 接线，shell 权限等具体策略全部以插件落地。本文把 hooks 模块本身
> （契约、分发语义、接线点、错误策略、测试与分期实施）一次设计完整。
>
> 现状：`core/hooks.py` 是一个无参无返回、从未被调用的 `Hooks` 桩类，
> 方法名还有拼写错误（`before_interation`）。本方案整体重写该文件。
>
> **现状（已实现，取代本文的分期计划）**：hook 面已收缩为**单点位**
> `before_tool_call`。其余五个点位（`before_turn` / `before_iteration` /
> `after_llm_response` / `before_tool_result` / `before_msg_out`）及其
> Decision 类已删除——全仓库零消费者（唯一真实调用点是 `core/runner.py`
> 的 `hooks.before_tool_call`）。需要时再加，成本是"一个方法 + 一个
> Decision 类"。
>
> 权限守卫见 `zero/permission/guard.py`，包络判定与 macOS 后端见 `zero/permission/`；
> 本文第 5 节的插件目录 `plugins/shell_guard/` 已整体删除。

## 1. 设计目标与原则

1. **核心零策略**：核心只定义"何时触发、传什么、返回什么、如何否决/替换"，
   不关心任何具体策略。无插件注册时，所有 hook 是 no-op，
   运行时行为与现状**逐字节一致**。
2. **决策对象而非 bool**：每个拦截型 hook 返回一个 Decision 对象，
   携带 `reason`（LLM/用户可见）与可选的替换载荷，为未来的
   mock/缓存/改写类 hook 留出空间。
3. **handler 一律 async**：审批交互（等待用户输入）可以直接在 handler 里 await。
4. **单点接线**：每个 hook 在 runner/loop 中只有一个调用点，不扩散。
5. **异常隔离**：任何 handler 抛异常只记日志、视为 no-op，绝不拖垮 agent
   loop；需要 fail-closed 的场景由插件在正常返回路径里返回否决决策。
6. **一次设计、分期接线**：契约一次定义完整，接线按需求分期进行，
   未接线的 hook 保持文档化预留，不提前改核心。

## 2. 生命周期与 hook 全景

一条消息在系统内的完整路径（`AgentLoop.run` → `AgentRunner._run_loop`）：

```
msg_in 出队
  │
  ├─▶ ① before_turn ────────────── 可改写消息内容 / 直接拒绝本轮
  │
  ├─ SessionStore.append(msg_in) / auto_title / 构建 Context
  │
  ├─ AgentRunner._run_loop（每个 iteration）:
  │     │
  │     ├─▶ ② before_iteration ─── 可注入/裁剪 payload，或中止循环（预算/防死循环）
  │     │
  │     ├─ 构建 LLMPayload → provider.stream_chunks
  │     │
  │     ├─▶ ③ after_llm_response ─ 观察/改写本轮文本与 tool_calls（审计、统计、检测）
  │     │
  │     ├─ 无 tool_calls → yield done，循环结束
  │     │
  │     └─ 有 tool_calls → 逐个执行（_execute_and_persist）:
  │           │
  │           ├─▶ ④ before_tool_call ── 可否决（reason 作为工具错误回给 LLM）
  │           │                         或替换结果（mock/缓存短路）
  │           ├─ tool.run(...)
  │           ├─▶ ⑤ before_tool_result ─ 改写/脱敏/增强工具结果
  │           ├─ 截断（10000 字符，核心安全不变量，始终在 hook 之后执行）
  │           └─ MessageTool 持久化
  │
  ├─ 汇总 reply_text → 组装 MessageOut
  │
  ├─▶ ⑥ before_msg_out ─────────── 改写/脱敏最终回复，或抑制发送
  │
  └─ SessionStore.append(msg_out) → out_queue.enqueue
```

| # | Hook | 触发时机 | 类型 | 典型用途 |
|---|---|---|---|---|
| ① | `before_turn` | msg_in 出队后、持久化前 | 拦截+改写 | 速率限制、消息预处理、忽略特定来源 |
| ② | `before_iteration` | 每次 LLM 调用前 | 拦截+改写 | token 预算熔断、注入临时上下文、动态裁剪工具 |
| ③ | `after_llm_response` | 一轮流式输出结束后 | 观察+改写 | 审计日志、用量统计、死循环检测、内容过滤 |
| ④ | `before_tool_call` | kwargs 解析后、`tool.run` 前 | 拦截+替换 | shell 权限守卫（首个插件）、mock/缓存 |
| ⑤ | `before_tool_result` | 工具执行后、截断+持久化前 | 改写 | 结果脱敏、压缩、附加元信息 |
| ⑥ | `before_msg_out` | MessageOut 组装后、持久化+入队前 | 改写+抑制 | 回复脱敏、追加签名、抑制后台 turn 的空回复 |

命名修正：`before_interation` → `before_iteration`；
`after_llm_ret` → `after_llm_response`（语义更明确）。

## 3. 契约（core/hooks.py，单文件，目标 <200 行）

### 3.1 HookContext

所有 hook 共用的运行时上下文，一次构建、逐 hook 透传：

```python
@dataclass
class HookContext:
    channel: str
    session_id: str
    turn_id: str = ""          # 触发本轮的 MessageIn.id
    iteration: int = 0         # 仅 iteration 级 hook 有意义
    tool_context: ToolContext | None = None
    metadata: dict = field(default_factory=dict)  # 从 msg_in.metadata 复制
```

### 3.2 Decision 类型

拦截型与改造型分开，返回 `None` 一律等价于"不干预"：

```python
@dataclass
class TurnDecision:            # ①
    proceed: bool = True
    reason: str = ""           # 拒绝时可选：作为一条普通回复发回（silent=False）
    silent: bool = False       # True = 整轮静默丢弃（如定时任务节流）
    message: MessageIn | None = None   # 非 None 时替换原始消息（内容改写）

@dataclass
class IterationDecision:       # ②
    proceed: bool = True
    final_text: str = ""       # proceed=False 时以此文本结束本轮（yield done）
    extra_messages: list[dict] = field(default_factory=list)  # 追加到 payload.messages
    exclude_tools: list[str] = field(default_factory=list)    # 本轮临时禁用的工具名

@dataclass
class LLMResponseDecision:     # ③
    content: str | None = None        # 非 None 时替换本轮 assistant 文本
    tool_calls: list[ToolCall] | None = None  # 非 None 时替换 tool_calls（慎用）

@dataclass
class ToolCallDecision:        # ④（沿用 v4）
    proceed: bool = True
    reason: str = ""           # 否决时作为工具错误返回给 LLM
    result: dict | None = None # 替换工具结果（mock/缓存短路）

@dataclass
class ToolResultDecision:      # ⑤
    content: str | None = None # 非 None 时替换工具结果 JSON 文本

@dataclass
class MessageOutDecision:      # ⑥
    content: str | None = None # 非 None 时替换回复文本
    suppress: bool = False     # True 时不持久化、不入队
```

### 3.3 Hooks 基类（默认 no-op）

```python
class Hooks:
    async def before_turn(self, msg: MessageIn, ctx: HookContext) -> TurnDecision | None: ...
    async def before_iteration(self, payload: LLMPayload, ctx: HookContext) -> IterationDecision | None: ...
    async def after_llm_response(self, content: str, tool_calls: list[ToolCall], ctx: HookContext) -> LLMResponseDecision | None: ...
    async def before_tool_call(self, tool_name: str, kwargs: dict, ctx: HookContext) -> ToolCallDecision | None: ...
    async def before_tool_result(self, tool_name: str, kwargs: dict, result: str, ctx: HookContext) -> ToolResultDecision | None: ...
    async def before_msg_out(self, msg: MessageOut, ctx: HookContext) -> MessageOutDecision | None: ...
```

### 3.4 HookChain（多插件分发）

```python
class HookChain(Hooks):
    def __init__(self):
        self._handlers: dict[str, list[Callable]] = defaultdict(list)

    def on(self, hook_name: str, fn: Callable) -> None: ...
    def on_before_tool_call(self, fn): ...   # 各点位的便捷注册，仅语法糖
```

分发语义按 hook 类型分三种，写死在 HookChain 里、对插件透明：

| 类型 | 涉及 hook | 链语义 |
|---|---|---|
| 否决型 | ① ② ④ | 按注册顺序执行，**首个否决/中止胜出**，后续 handler 不再调用 |
| 管道型 | ③ ⑤ ⑥ | 每个 handler 的替换输出作为下一个 handler 的输入，最终生效 |
| 异常 | 全部 | 单 handler 异常 → `logger.exception` + 视为返回 None，链继续 |

`on()` 接受纯函数，也接受 `Hooks` 子类实例（自动绑定同名方法），
插件两种写法都可以。

## 4. 核心接线（4 个文件的一次性改动）

### 4.1 core/runner.py（② ③ ④ ⑤）

- 构造：`AgentRunner(..., hooks: Hooks | None = None)`，`None` → `Hooks()`。
- ② 在 `_run_loop` 的 for 循环顶部、构建 payload 之后、调用 provider 之前；
  `proceed=False` → `yield StreamEvent(kind="done", content=final_text)` 并 return。
- ③ 在流式循环结束、`tool_calls` 解析之后；替换结果照常参与后续
  "无 tool_calls → done" 的判断与持久化。
- ④⑤ 都在 `_execute_and_persist` 内：

```python
kwargs = json.loads(tc.function.arguments or "{}")
decision = await self.hooks.before_tool_call(tool.name, kwargs, hook_ctx)
if decision and not decision.proceed:
    content = json.dumps({"error": decision.reason}, ensure_ascii=False)
elif decision and decision.result is not None:
    content = json.dumps(decision.result, ensure_ascii=False)
else:
    result = await tool.run(context=self.context.tool_context, **kwargs)
    content = json.dumps(result, ensure_ascii=False)
    modified = await self.hooks.before_tool_result(tool.name, kwargs, content, hook_ctx)
    if modified and modified.content is not None:
        content = modified.content
if len(content) > 10000:                    # 截断永远在 hook 之后
    content = content[:10000] + "...truncated"
```

否决/替换照常走 MessageTool 持久化——LLM 与会话历史都能看到，
hook 语义对 runner 其余逻辑完全透明。

### 4.2 core/loop.py（① ⑥ + 透传）

- 构造透传 `hooks`，并注入每个 `AgentRunner`。
- ① 在 dequeue 之后、`SessionStore.append(msg_in)` 之前（拒绝的消息不留痕）；
  拒绝且非 silent 时，把 `reason` 组装成一条普通 MessageOut 发回。
- ⑥ 在最终 `SessionStore.append(msg_out)` 之前（脱敏结果与持久化历史一致）。

### 4.3 main.py（组合根）

```python
def build_hooks() -> Hooks:
    chain = HookChain()
    try:
        from plugins.shell_guard import install
        install(chain)
    except ImportError:
        pass                      # 插件不存在 = 纯 no-op
    return chain
```

### 4.4 不动的部分

`bus/`、`channels/`、`tools/`、`models.py`（仅新增 import）、
`config.py`、`session/` 全部不动。

**明确排除**：runner 中间态的 assistant+tool_calls 持久化
（`_persist_assistant_with_tools`）不挂 hook——hook 只作用于对 LLM/用户
可见的内容流，持久化内部细节不留扩展点，避免过度设计。

## 5. 错误策略与边界

- **handler 异常**：记日志、当 None 处理（fail-open）。护栏类插件要
  fail-closed 时，由插件在正常返回路径返回否决（如审批通道不可用 → deny），
  而不是靠抛异常。
- **HookContext 的构建成本**：复用已有的 ToolContext/metadata 引用，不做深拷贝。
- **插件内交互**：CLI 审批这类 mid-turn 交互由插件 handler 自己 await
  （v4 已定：CLI 用 prompt_toolkit 就地确认，飞书/定时任务 fail-closed），
  核心不提供 UI 通道。
- **威胁模型**：防模型手滑的护栏，不是安全沙箱；OS 级隔离是后续可选层。
- **hook 不提供**：动态卸载、优先级排序、跨 turn 状态管理
  （插件自己要状态就自己持有，核心不管）。

## 6. 用例 → hook 映射（验证设计覆盖面）

| 需求 | 落点 | 机制 |
|---|---|---|
| shell 命令三态守卫 | ④ | 否决 + CLI 审批 |
| 审计日志（全量工具调用留档） | ④⑤ | 观察，返回 None |
| LLM 用量/耗时统计 | ③ | 观察 |
| 定时任务结果轰炸节流 | ① | silent 丢弃 |
| 回复/工具结果敏感信息脱敏 | ⑤ ⑥ | 管道改写 |
| token 预算熔断 | ② | 中止循环 + final_text |
| 防死循环（同一工具 N 次相同参数） | ③/④ | 观察计数 + 否决 |
| 工具结果 mock（测试） | ④ | result 替换 |

## 7. 测试计划（tests/test_hooks.py）

1. **回归**：无插件 HookChain 跑一轮含工具调用的完整 turn，断言持久化
   消息序列与现状一致。
2. **否决短路**：两个 handler，第一个否决，断言第二个未被调用、
   `tool.run` 未执行、MessageTool 内容为 `{"error": reason}`。
3. **管道顺序**：③⑤⑥ 各注册两个改写 handler，断言按注册顺序串联生效。
4. **异常隔离**：handler 抛异常，断言 loop 正常完成、日志有记录。
5. **截断不变量**：hook 返回超长替换结果，断言仍被截到 10000。
6. ② 中止：断言 yield done 且不再调用 provider。

## 8. 分期实施

| 期 | 内容 | 验收 |
|---|---|---|
| P1 | `core/hooks.py` 全量契约（第 3 节）+ ④ 接线 + 构造透传 + 组合根；测试 1/2/4/5 | shell_guard 插件可落地，无插件行为不变 |
| P2 | `plugins/shell_guard/`（按 v4 第 5 节） | CLI 审批链路走通 |
| P3 | ① ⑥ 接线（loop.py 两个点）+ 测试 3 | 有真实插件需求即触发 |
| P4 | ② ③ 接线 + 测试 6 | 有预算/审计需求即触发 |

P1 是唯一需要立即动核心的部分；P3/P4 的契约已经定义好，
届时接线每处不过十几行。

## 9. 待确认的开放问题

1. ① `before_turn` 拒绝时，scheduler 来源的消息（metadata 有 source/task_id）
   是否允许 silent？（倾向：允许，由插件判断 metadata。）
2. ③ 是否允许替换 tool_calls？能力先留着，但文档标注"慎用"，
   错误替换会破坏 tool_call_id 配对。
3. HookChain 是否需要 `off()` 注销？（倾向：暂不需要，进程级组合根注册。）
