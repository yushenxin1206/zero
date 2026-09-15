# Shell 命令执行权限方案（v4：先实现通用 Hooks，再以 hook 插件落地）

> v3 结论修正：注册表包装器是"零侵入"的极限，但每个拦截需求都要单独
> 包装工具，没有通用扩展点。更合理的分工是——**核心做一次通用的 hooks
> 接线（不含任何权限策略），shell 权限作为第一个 hook 插件落地**。
> hooks 基础设施同时服务未来的审计日志、速率限制、其他工具守卫。

## 1. 核心与插件的分工

| 侧 | 内容 | 策略含量 |
|---|---|---|
| 核心（一次性改动） | `core/hooks.py` 契约重设计、`core/runner.py` 单点调用、构造参数透传 | 零——只定义"何时问、怎么否决"，不关心"问什么" |
| 插件 `plugins/shell_guard/` | 三态判定、审批交互、规则沉淀、自有配置 | 全部 |

核心改动后默认行为与现状**逐字节一致**（无插件注册时 hook 是 no-op）。

## 2. Hook 契约设计（core/hooks.py）

现有 `Hooks` 类的方法无参数无返回，从未被调用——重新设计为：

```python
@dataclass
class ToolCallDecision:
    proceed: bool = True
    reason: str = ""                # 否决时作为工具错误返回给 LLM
    result: dict | None = None      # 可选：替换工具结果（供 mock/缓存类 hook）

class Hooks:
    """默认 no-op；插件通过 HookChain 注册 handler。"""
    async def before_tool_call(
        self, tool_name: str, kwargs: dict, context: ToolContext
    ) -> ToolCallDecision:
        return ToolCallDecision()

class HookChain(Hooks):
    """多插件分发：任一 handler 否决即否决，首个否决胜出。"""
    def __init__(self):
        self._before_tool_call: list[Callable] = []
    def on_before_tool_call(self, fn): ...
```

设计要点：

- **只加 `before_tool_call` 一个真正的拦截点**，其余预留方法（before_iteration 等）不在本次接线，避免为假想需求改核心
- 返回 `ToolCallDecision` 而非 bool：`reason` 直接成为 LLM 可见的工具错误，`result` 预留给未来的替换型 hook
- handler 是 async 的，审批交互（等待用户输入）可以直接在里面 await

## 3. runner 的单点接线（core/runner.py）

`_execute_and_persist` 内，kwargs 解析之后、`tool.run` 之前插入唯一调用点：

```python
kwargs = json.loads(tc.function.arguments or "{}")
decision = await self.hooks.before_tool_call(
    tool.name, kwargs, self.context.tool_context)
if not decision.proceed:
    content = json.dumps({"error": decision.reason}, ensure_ascii=False)
elif decision.result is not None:
    content = json.dumps(decision.result, ensure_ascii=False)
else:
    result = await tool.run(context=self.context.tool_context, **kwargs)
    ...
```

否决/替换结果照常走 MessageTool 持久化，LLM 和会话历史都看得到——
hook 语义对 runner 的其他逻辑完全透明。

## 4. 构造透传（core/loop.py + main.py）

- `AgentRunner(..., hooks: Hooks | None = None)`，None 时用默认 no-op `Hooks()`
- `AgentLoop` 同样透传
- 组合根 main.py：创建 `HookChain`，留给插件注册，注入 loop

```python
# main.py · build_agent_loop
hooks = HookChain()
from plugins.shell_guard import install
install(hooks)          # 插件注册自己的 before_tool_call handler
```

核心文件改动清单：`core/hooks.py`（重设计）、`core/runner.py`（一个调用点）、
`core/loop.py`（一个透传参数）、`main.py`（组合根接线）。
`bus/`、`channels/`、`tools/shell.py`、`datacls.py`、`config.py` 不动。

## 5. 插件侧（与 v3 基本相同，拦截点从包装器换成 hook）

```
plugins/shell_guard/
├── __init__.py      # install(hooks)：注册 before_tool_call handler
├── handler.py       # tool_name != "shell" → 放行；否则走判定/审批
├── policy.py        # 三态判定纯函数（v2 逻辑）
├── store.py         # ~/.zero/shell_guard.json：规则 / 超时 / 追加白名单
├── approval.py      # CLI 就地审批（run_in_terminal）/ 无人值守渠道 fail-closed
└── manage.py        # zero-guard list / add / remove
```

handler 伪代码：

```python
async def shell_guard(tool_name, kwargs, context) -> ToolCallDecision | None:
    if tool_name != "shell":
        return None                       # 不关心的工具直接放行
    d = policy.evaluate(kwargs["command"], kwargs.get("working_dir"), store.rules)
    if d.tier == "allow":
        return None
    if d.tier == "deny":
        return ToolCallDecision(proceed=False, reason=f"blocked: {d.reason}")
    ok = await approver.ask(context, kwargs["command"], d.reason)
    return None if ok else ToolCallDecision(proceed=False, reason="denied by user")
```

审批交互方案与 v3 一致：CLI 用 prompt_toolkit 原语就地弹确认
（y/n/a/w），飞书与定时任务 fail-closed 并引导预置规则。

## 6. 与包装器方案（v3）的诚实对比

| 维度 | v3 注册表包装器 | v4 hooks |
|---|---|---|
| 核心改动 | 零（组合根一行） | 4 个文件、一次性、通用 |
| 扩展性 | 每个新需求再包一层，层层嵌套 | 所有横切需求共用一条 hook 链 |
| 语义清晰度 | 插件伪装成工具本身 | "策略在工具之外"显式表达 |
| 卸载成本 | 删一行 | hooks 留下（通用资产），插件注销即可 |
| 风险 | monkey 覆盖语义隐晦 | hook 契约需要一次设计到位 |

结论：愿意做一次小的核心接线，就选 hooks；它把"权限"从某个工具的
实现细节提升为系统级横切能力。

## 7. 威胁模型（沿用）

防模型手滑的护栏，非安全沙箱；OS 级隔离留作后续可选层。

## 8. 实施步骤

1. `core/hooks.py`：`ToolCallDecision` + `Hooks` no-op + `HookChain`
2. `core/runner.py`：单点接线 + 否决结果的 MessageTool 持久化
3. `core/loop.py` / `main.py`：hooks 透传与组合根接线；回归测试确认无插件时行为不变
4. `plugins/shell_guard/`：policy.py + 单测 → handler → CLI 审批 → manage.py
5. prompt.md 引导 + 文档收尾
