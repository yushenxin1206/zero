# Hooks 模块

> 现状文档（与 `zero/core/hooks.py` 一致）。历史上的六点位设计已放弃：
> hook 面收缩为**单点位** `before_tool_call`，`plugins/shell_guard/` 已删除，
> 权限守卫在 `zero/permission/guard.py`，由 `main.build_hooks` 装入。

## 1. 为什么只有一个点位

唯一真实的 hook 消费者是权限守卫，它挂在「工具参数解析后、`tool.run` 前」。
这个位置天然满足守卫的两类需求：

- **否决**：`ToolCallDecision.proceed=False`，`reason` 作为工具错误回给 LLM；
- **长等待**：`HookContext.stop_event` 让审批等待与用户终止竞争。

其余五个点位（`before_turn` / `before_iteration` / `after_llm_response` /
`before_tool_result` / `before_msg_out`）全仓库零消费者，已删除。需要时再加，
成本是「一个方法 + 一个 Decision 类」。

## 2. 契约

### 2.1 HookContext

```python
@dataclass
class HookContext:
    channel: str
    session_id: str
    turn_id: str = ""                        # 触发本轮的 MessageIn.id
    tool_context: ToolContext | None = None
    stop_event: asyncio.Event | None = None  # 当前 turn 的终止信号
```

### 2.2 决策

返回 `None` 一律等价于「不干预」。

```python
@dataclass
class ToolCallDecision:
    proceed: bool = True
    reason: str = ""      # 否决时作为工具错误返回给 LLM
    result: dict | None = None  # 替换工具结果（mock/缓存短路）
    note: str = ""        # 附注：proceed=True 时并入工具结果，随 tool 消息进上下文
```

### 2.3 类型别名与名单

```python
HookName = Literal["before_tool_call"]
HookHandler = Callable[[str, dict, HookContext], Awaitable[ToolCallDecision | None]]
HOOK_NAMES: tuple[HookName, ...] = ("before_tool_call",)
```

`HOOK_NAMES` 与 `Hooks` 基类的公开方法集合必须一致，由
`tests/test_hooks.py::test_hook_names_match_base_methods` 锁定。

### 2.4 Hooks 基类

默认 no-op：

```python
class Hooks:
    async def before_tool_call(
            self, tool_name: str, kwargs: dict, ctx: HookContext,
    ) -> ToolCallDecision | None:
        return None
```

### 2.5 HookChain

```python
class HookChain(Hooks):
    def on(self, hook_name: HookName, fn: HookHandler) -> HookHandler: ...
    def register(self, hooks: Hooks) -> None: ...      # 只接管覆写过的方法
    def on_before_tool_call(self, fn: HookHandler) -> HookHandler: ...
    def handlers_for(self, hook_name: HookName) -> tuple[HookHandler, ...]: ...
```

- `on()` 可作装饰器使用；`register()` 接收 `Hooks` 子类实例，用绑定的方法
  注册，因此签名校验看到的是去掉 `self` 后的三个位置参数。
- **注册期校验**：handler 必须是 `async def`，且参数个数与 `Hooks` 基类
  同名方法一致（不含 `self`，当前为 3 个；声明 `*args` 可豁免）。期望个数
  由基类签名推导（`_expected_arity`），新增点位无需改校验。签名漂移若留到
  分发期，会被 `_safe` 的宽 `except` 吞成静默 no-op——对 fail-open 的护栏
  来说等于护栏不存在，所以在注册处直接 `TypeError`。

## 3. 分发语义

- 按注册顺序执行；**首个否决/替换决策胜出**，后续 handler 不再调用。
- 此前 handler 返回的 `note` 会累积，并在短路时一并带出。
- 全部 handler 只返回 `note` 时，合并成一条 `ToolCallDecision(note=...)`；
  否则返回 `None`。
- handler 异常：`logger.exception` + 视为返回 `None`，链继续（fail-open）。
  唯一例外是 `TurnCancelled`——它是终止信号而非业务异常，必须原样透传，
  否则用户终止会被吞成「不干预」。

## 4. 接线

`hooks` 是**必填**参数，不再有 `hooks or Hooks()` 的静默兜底：

| 组件 | 参数形态 |
|---|---|
| `AgentLoop(...)` | keyword-only `hooks: Hooks` |
| `AgentRunner(...)` | keyword-only `hooks: Hooks` |
| `SubagentManager(...)` | keyword-only `hooks: Hooks` |
| `run_subagent(prompt, *, config, hooks: Hooks, ...)` | 必填 |

唯一调用点是 `AgentRunner._execute_and_persist`（参数解析后、`tool.run` 前）。
`AgentLoop` 把同一条链注入每个 `AgentRunner`；子 agent 与主对话共用同一条链。

## 5. 组合根

```python
def build_hooks(msg_bus: MessageBus,
                permission: PermissionState) -> Hooks:
    chain = HookChain()
    from zero.permission.guard import install
    install(chain, msg_bus, permission=permission)
    assert chain.handlers_for("before_tool_call"), \
        "permission guard 未注册到 hook 链：shell 权限将 fail-open"
    return chain
```

守卫装不上（import 失败、`install` 提前返回）时启动期直接失败，而不是带着
空链运行到第一条危险命令。`tests/test_hooks.py::test_build_hooks_requires_guard`
覆盖这条断言。

## 6. 测试

`tests/test_hooks.py` 覆盖：无插件时链路为 no-op、否决短路与 note 合并、
handler 异常隔离、`TurnCancelled` 透传、截断不变量、`HOOK_NAMES` 与基类
一致性、同步 handler / 参数个数不符在注册期被拒、`handlers_for` 顺序、
`build_hooks` 的守卫断言。
