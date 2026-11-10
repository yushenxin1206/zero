# Zero 架构改造结果（2026-11-10）

> 对应评审：[architecture-review.md](architecture-review.md)（评审日期
> 2026-10-22，含 §0.4 二次复审）。评审本身作为背景资料保留，不随本文更新。
>
> 本文记录按该版评审做了什么、关键决定与取舍、对应提交、验证结果，以及
> 明确不做的项和仍待办项。结论以当前代码与测试为准，不写会漂移的行号，
> 只写文件与符号名。

## 1. 结论总表

| 编号 | 问题 | 结论 | 关键证据 |
|---|---|---|---|
| P0-1 | 通道层无共享服务、CLI God Class | 已完成 | `channels/service.py`；CLI 拆为 `channels/cli/` 包（c1b709b） |
| P0-2 | 全局单例与导入期副作用 | 已完成 | `DataPaths` 显式注入；无 `current()`/`configure()`；架构测试守住 |
| P1-0 | 读全开 × 网络全开 | 第一步已完成；第二、三步明确不做 | `current_envelope()` 按来源给 `network=attended(ctx)`；`docs/sandbox.md` 成文 |
| P1-1 | 持久化缺"一轮"事务边界 | 已完成 | `TurnMarker` 的 `turn_begin`/`turn_end`；`synthetic` 字段；读路径零写盘 |
| P1-2 | 同步 IO 阻塞事件循环、全量索引重写 | 已完成 | 阻塞 IO 全部 `asyncio.to_thread`；索引 mtime 缓存；写入按需 |
| P1-3 | 配置继承的"是否写过"语义与自毁开关 | 已完成 | `save_config` 已删除；`--write` 拒绝覆盖；骨架继承测试 |
| P1-4 | 工程护栏缺失、Python 版本钉死 | 已完成 | `.github/workflows/ci.yml`；`requires-python >=3.12`；ruff + pytest |
| P2-1 | core 依赖策略层；Provider 有状态 | 已完成 | core 无 `zero.permission` import；`sandbox_available` 纯布尔注入；流句柄下沉 |
| P2-2 | Provider 抽象名义化、死方法 | 已完成 | `chat_with_llm` 已删除；支持范围见 §2.8 |
| P2-3 | 没有可观测性 | 已完成 | 轮次摘要 + `turn_id`/`session_id` 日志注入（281abb2）；定时任务终态回写（0c59cc5） |
| P2-4 | 关键契约靠注释执法 | 已完成 | metadata AST 测试；`hooks` 必填；启动期守卫断言；通道归类测试 |
| P2-5 | 无界队列、迭代超限伪装 done | 已完成 | 四条队列有界 + `BusQueueFull`；超限改 `error` 事件 |

## 2. 已完成项

### 2.1 P0-1 通道层共享服务（c1b709b）

- 抽出 `ChannelService`（`zero/channels/service.py`）：会话列表/创建/切换/标题、
  模型查询与切换、任务列表、历史投影。三条通道都只调它。
- CLI 拆为 `zero/channels/cli/` 包：`channel.py` 只管生命周期与 bus 对接，
  渲染在 `renderer.py`，斜杠命令在 `commands.py`，输入线程在 `input.py`，
  审批面板在 `approval.py`，后台 turn 在 `background.py`。入口文件 384 行。
- **保留的差异**：CLI 仍在构造时接收 `SessionStore` / `SchedulerStore` 并注入
  子组件，通道内不直接读写；飞书通道保留一处 `resolve_session` 直接调用，
  用于"chat → 会话"路由。原验收标准写的是"不 import 任何 store"，实际口径
  是"通道不承载业务读写"，这两处 import 属于注入与路由，不是复制业务逻辑。

### 2.2 P0-2 全局单例与导入期副作用

- `zero/data_paths.py` 只做纯路径推导（`DataPaths` / `DataPaths.default()`）：
  没有进程级根，组合根与 CLI 入口显式构造并向下传。
  `test_data_paths_module_has_no_process_global` 守住这条约束。
- `SessionStore` / `SchedulerStore` 为实例类，构造时接收路径，由
  `main.build_core()` 注入 `AgentLoop`、`Context`、`ChannelService`、
  `MaintenanceEngine`、`Scheduler`。
- 权限层的探针缓存 / 本会话写根收进 `PermissionState`，`guard`、`Shell`、
  `Context` 显式接收；`seatbelt` 的 profiles 目录改为显式参数。
- 配置读取改为显式调用 `load_config(path)`，import 期没有任何文件 IO。
- 测试侧每例构造隔离 `DataPaths` 注入，不再 monkeypatch 模块常量。

### 2.3 P1-0 读 × 网络：切断无人值守（72c3cf1）

- 网络不再是无条件开关：`current_envelope(ctx)` 返回 `network=attended(ctx)`。
  `attended` 要求"交互渠道 + 用户来源"同时成立；scheduler / subagent /
  飞书等来源一律 `network=False`，profile 渲染为 `(deny network*)`。
- 审批能力与网络放行复用同一个"是否有人在场"判定，避免两套规则漂移。
- 网络与读拒绝一样做双向探针：基线命令必须仍可执行，未授权连接必须失败。
  测试见 `tests/test_permission.py` 的 `test_network_probe`、
  `test_network_policy_by_source`、`test_shell_network_by_source`。
- 决策已成文于 `docs/sandbox.md`（"读 × 网络外泄"一节），与 launchd 残留
  风险并列。

### 2.4 P1-1 轮次事务边界（c30b5f9）

- 会话文件在用户消息前写 `turn_begin`，在本轮最后一条消息后写 `turn_end`；
  读取方按段恢复：
  - 有 `turn_end` 的轮次：完整保留，缺 tool 结果时补 `synthetic=True` 占位；
  - 没有 `turn_end` 且不是当前活跃轮：视为崩溃残留，整轮不进模型上下文，
    不补占位、不写盘；
  - 没有标记的旧历史：按"已闭合"处理，只做内存补全。
- 修复不再靠固定文案识别；只有轮次所有者首次读历史
  （`repair_closed_turns=True`）才把已闭合段的补全结果原子写回，其它读路径
  零写盘。回归测试覆盖中断、占位与去重（`tests/test_cancel_hardening.py`）。

### 2.5 P1-2 同步 IO 与索引（c2d8ab4、aca2d1d、390b69f 等）

- loop、通道、维护任务的阻塞文件 IO 全部经 `asyncio.to_thread` 移出事件循环；
  停止信号不再被同步 IO 延迟。
- 会话索引只在内容真正变化时写；活跃时间不落盘，由 `messages.jsonl` 的
  mtime 推导，列表查询从平方级反查降为查表。
- 索引读-改-写用"进程内线程锁 + `.index.lock` 上的 `fcntl.flock`"两层保护，
  临时文件唯一化后原子替换。
- Windows 上的跨进程锁缺口见 §3.2。

### 2.6 P1-3 配置继承（521fdde、9b771d9、f139630）

- 删除了全字段回写的 `save_config`，配置没有运行时写入口。
- `python -m zero.config` 默认只打印全量参考；`--write` 只落"能跑起来"的
  精简骨架（models / providers / `agentRuntimes.default` 三处），目标已存在
  时拒绝覆盖。派生任务靠继承 default，不再被展开成全显式而永久失联。
- `test_config_write.py` 锁定：骨架不写死派生任务、改 default 后 compact 等
  跟随继承、已有文件时字节与 mtime 不变。

### 2.7 P1-4 工程护栏（c1b709b、2d3d118）

- CI 在 `.github/workflows/ci.yml`：push 到 `master`/`dev` 与 PR 触发，
  macOS 上跑 `uv sync --dev` + `ruff check .` + `pytest -q`，Python 3.12/3.13
  矩阵。
- `pyproject.toml`：`requires-python >=3.12`，dev 依赖组含 pytest / pytest-timeout
  / ruff，pytest 配置了 `testpaths`、`pythonpath` 与单例超时。
- 测试全部可由 pytest 收集，平台不适用用例 skip 而非 error；README 已按
  "macOS 主平台、Windows 兼容说明保留"改写。

### 2.8 P2-1 / P2-2 核心层与 Provider

- **P2-1**（3529f9c、448e40e、3922775）：沙箱能力事实在组合根结算成布尔值
  注入 `Context`，core 不再 import `zero.permission`（架构测试无豁免）；
  `OpenAI` 的流句柄下沉为单次调用的局部变量，同一实例并发两条流互不干扰
  （`tests/test_repro_token_stop_hang.py` 有回归）。提示词文档生成这类需要
  真实权限状态的脚本挪到 `scripts/gen_prompt.py`，不进 core。
- **P2-2**（90d4e22 等）：删除零调用的 `chat_with_llm` 抽象方法与实现。
  **支持范围（成文）**：当前只支持 OpenAI-compatible wire format——
  `LLMCallOptions` 字段与 `payload_dict` 直接对应 Chat Completions 协议，
  多个厂商靠 `providers.*.apiBase` 与 `models.*.provider` 区分，不在 Provider
  内部做协议归一化。接原生厂商 API 前，先定义 payload / 流事件归一化边界与
  fake provider 一致性测试。

### 2.9 P2-4 契约自动执法

- metadata 只作为第三方扩展口袋：`tests/test_architecture.py` 用 AST 扫描
  `core/`、`channels/`、`scheduler/`，白名单为空。压缩消息改由
  `MessageBody.compact` 类型化字段表达，不再读 metadata。
- `hooks` 参数必填；组合根 `build_hooks()` 启动期断言权限守卫已挂到
  `before_tool_call`，装不上直接失败，不静默 fail-open。
- 通道能力有覆盖测试：扫描 `zero/channels` 下所有 `CHANNEL` 声明，要求每个
  通道都在 `ATTENDED_CHANNELS` 或测试内的 `UNATTENDED_CHANNELS` 里。

### 2.10 P2-5 队列与迭代上限（c70ce4c）

- 四条队列全部有界（in 64 / out 256 / stream 1024 / stop 16），新增
  `BusQueueFull`；三种入队语义显式化——`enqueue` 等待（背压）、
  `enqueue_nowait` 拒绝、`enqueue_latest` 丢最旧。
- 通道路由队列用 `enqueue_latest`：慢通道不能反压核心，丢最旧会记 error；
  终稿已落盘，流式以最终 `done` 为准。
- runner 的 `max_iterations` 耗尽改为 `error` 事件，子 agent 路径转
  `RuntimeError`；失败轮不把提示文案落盘为 assistant 正文，重载后只保留
  用户消息。

### 2.11 P2-3 可观测性与定时任务完成态（281abb2、0c59cc5）

- **轮次摘要**：每轮结束落一行 `turn status=...`，含 `turn_id`、`channel`、
  `session`、`model`、耗时、工具次数、token 与错误原因。
- **日志上下文**：`zero/utils/log_context.py` 用 `contextvars` 保存
  `turn_id` / `session_id`，`AgentLoop` 在轮次边界进出上下文
  （`turn_log_context`）；`log_setup` 的 `TurnContextFilter` 统一给日志行
  加 `[turn=... session=...]` 前缀。调用点不新增参数，`asyncio.to_thread`
  里的写盘与工具日志同样携带。
- **定时任务终态**：派发入队只把任务置为 `running`，轮次真正结束后由
  `Scheduler.finish_turn` 回写；once 落 `completed` / `failed` / `cancelled`，
  recurring 保持 `pending` 并更新 `last_run_at`。本轮成败只由轮次摘要日志
  记录，任务文件不再冗余存一份结果。
  进程内 `_in_flight` 集合阻止 running 任务被重复派发；进程中途崩溃时
  once 停留在 running，重启后仍会被 `find_due` 重跑（at-least-once）。

## 3. 明确不做 / 接受的风险

### 3.1 读 × 网络：只切断无人值守（P1-0 第二、三步不采纳）

当前决策是"交互渠道的用户轮次保留网络，无人值守来源断网"。不继续做
"attended 也默认拒绝、按二进制授予网络"和"shell 彻底禁网 + 独立 http_fetch
工具"，理由：

- 交互轮次里 web-search、git、pip、npm 都依赖网络，全局默认拒绝会让 agent
  基本不可用；按二进制维护白名单的成本高于当前收益。
- 把"任意程序执行"与"对外通信"分离是架构级改动，没有需求驱动。
- 当前安全目标明确为"防手滑、限制破坏半径"，不是对抗提示注入；
  `docs/sandbox.md` 已把这条决策连同 loopback 未隔离一起成文。

### 3.2 Windows 上没有跨进程索引锁（接受的风险）

索引写入靠两层保护叠加：

1. **进程内**：每个 channel 一把 `threading.Lock`，把同一进程里多个线程的
   "读索引 → 改一条 → 整份写回"串行化；
2. **跨进程**：索引同目录的 `.index.lock` 文件上做 `fcntl.flock` 独占，
   防止两个进程同时执行同一段 read-modify-write。

`fcntl` 是 POSIX 模块，Windows 上 import 直接失败，`session/store.py` 里
退化为 `fcntl = None`，即**只剩第一层**。后果不是文件损坏——索引始终经唯一
临时文件 + 原子替换落盘，任何时刻读到的都是完整 JSON——而是**丢更新**：
两个进程同时创建会话或改标题/模型时，后写者基于旧快照整份覆盖，先写者的
条目会消失（last-writer-wins）。

适用前提与用户侧注意事项：

- 当前产品形态是**单进程持有数据根**：CLI、`--web` 桌面窗口、飞书通道、
  调度器都在同一个 `zero` 进程内，进程内锁足以覆盖。
- 因此 Windows 上**不要同时启动两个 `zero` 进程指向同一个数据根**（典型
  误用：一个终端跑 CLI，另一个再开桌面窗口）。需要并行时只能换一个用户账户
  （数据根固定为 `~/.zero`，没有环境变量或启动参数可覆盖），或只保留一个进程。
- 进程被强杀不会损坏索引（原子替换保证），最坏情况是最后一次未落盘的索引
  变更丢失。
- 真要补这一层，方向是 Windows 命名互斥体或 `msvcrt.locking`，或引入单实例
  进程锁；目前不做——它会把平台分支和额外依赖带进存储层，而"多进程共写同一
  数据根"并非当前支持的用法。

### 3.3 其它明确不做

- **飞书单会话**：一个会话群只对应一个会话，不做群内多会话切换。这是产品
  选择，不是能力缺口。
- **可观测性面板**：不做 `/debug last` 或 Web debug 帧。轮内日志已带
  `turn_id` / `session_id`，排障用 grep 已够；没有仪表盘需求。
- **`provider_for` 工厂**：runner 仍直接构造 `OpenAI(...)`，入参是配置层
  结算后的 `ProviderConfig` 纯数据，`LLMRuntime` 只含 provider 与窗口。
  再抽一层工厂不增加隔离，只增加间接。
- **历史编号 P2-6 / P2-7**：这两个编号在文档与提交历史中都没有定义，也没有
  对应需求，不再沿用。若后续出现新问题，按现有 P 编号续编。

## 4. 仍待办

截至 2026-11-10，该版评审列出的条目已全部处理，**无待办项**。

下一轮评审会另起评审文档，其结论与处置再增补到本文对应的新版本，而不是
改写本轮记录。

## 5. 验证方式与结果

```bash
uv run ruff check .
uv run pytest -q
```

- 当前结果：`ruff` 通过；`pytest` **183 passed, 2 skipped**（skip 为
  Windows 专用按键用例）。
- CI 在 `.github/workflows/ci.yml` 跑同一组命令（macOS、Python 3.12/3.13）；
  PR 上必须看到绿色。
- 涉及权限的用例依赖 Seatbelt，只在 macOS 可跑；Windows 专用用例在 macOS 上
  自动 skip。
- 删除关键守卫（例如组合根里的权限挂钩、存储原子替换）应让测试或启动失败，
  这是本文各"已完成"项的最低验证标准。
