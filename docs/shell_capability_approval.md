# Shell 执行审批：能力授权模式（v1，借鉴 Apple App Sandbox）

> 本文替换 `docs/shell_permission.md`（v4，三态命令判定）与
> `docs/sandbox.md`（v2，四档 Seatbelt 计划）中的**审批语义**部分。
> 目标只有一个：把"逐条命令做字符串判定 + 六选项审批"换成
> "声明能力包络 + 内核强制 + 越界能力授权"。
>
> 结论先行：**审批的对象从「命令」变成「能力」**——
> 一条能力 = （资源类别 × 目标 × 时效）。命令文本不参与授权判定。

> **实现状态（阶段 A + B 均已落地）**：`zero/sandbox.py` + `zero/sandbox_guard.py`
> 已实现包络、profile 渲染、`wrap`、双向探针、`check_readable`/`check_writable`
> 与 P/G/D 裁定；`plugins/` 整目录已删除；CLI 为 `zero-sandbox`。
> `read`/`write`/`edit` 越界也已接入同一审批门（阶段 B），不再只是 fail-closed。
> 审批面板：扩权 3 键（`s` 本会话 / `a` 总是 / `n` 拒绝），脱离沙箱与无沙箱降级
> 2 键（`y` 允许一次 / `n` 拒绝）——本文第 3 节的 6 选项面板与 §13 的六模块改造
> 清单作废，实际改动见 `docs/sandbox.md` 顶部的 v3 说明。

## 0. 决策记录

| 议题 | 决定 | 理由 |
|---|---|---|
| 判定对象 | **资源能力**，不是命令字符串 | `git` / `npm` / `python` 单看命令毫无风险信息量：`git status` 与 `git push --force` 天差地别（见 §1） |
| 判定时机 | **执行前只做一件事：查有无越界能力申请**；是否真越界由内核裁决 | 预执行无法可靠预测 `python -c "open('…','w')"`；"猜"是错的接缝（见 §1） |
| 强制点 | **内核 Seatbelt**（macOS）；应用层只做提示，不做安全边界 | 应用层判定可被绕过，内核不可绕过 |
| 授权粒度 | 授权 = 一条 **scoped token**（资源 × 时效），不是一条命令豁免 | 镜像 Apple 的 powerbox / security-scoped bookmark：把"能力"交出去，而非"信任进程" |
| 审批选项 | **4 个**，沿"时效"一条轴：本次 / 本会话 / 总是 / 拒绝 | 取消现状 6 个正交乱项（y/a/s/t/n/d），用户不必理解内部状态机 |
| 会话状态 | **单一维度**：会话级 token 表 | 删除"会话 TTL + 轮次 TTL + 惰性过期 + 轮次切换清空"四套并存状态（见 §8） |
| 危险命令清单 | **删除** 20 条 `DEFAULT_DENY` glob 与 27 条 `DEFAULT_ALLOW` glob | 它们表达的危险（`rm -rf /`、`dd of=/dev/*`、`sudo`）改由"写集合不含 `/`、无 privilege、无 raw device"表达（见 §6） |
| 不可授予集合 | 用**能力类别**黑名单，不用命令黑名单 | 凭证读、系统路径写、策略文件写属"此类能力永不授予"，与命令无关 |
| 无人值守 | 收敛为一句："该能力申请无人受理 → fail-closed" | 删除 scheduler / subagent / 其他渠道三套特例文案与 origin 分支 |
| 配置面 | **profile 文件**（声明式资源声明），零命令模式 | 配置即包络本身，不再维护规则引擎（见 §4） |

## 1. 现状诊断：为什么复杂，且效果不好

现有链路：`runner.py` → `hooks.before_tool_call` → `shell_guard` 三态
（deny > allow > ask，`plugins/shell_guard/policy.py`）→ ask 时 CLI 就地审批。
四个模块（policy / handler / approval / store）+ hook 链 + origin 分支。

| # | 问题 | 根因 | 后果 |
|---|---|---|---|
| P1 | **假安全** | 规则匹配的是命令前缀 glob | 放行 `git *` 等于放行 `git` 的全部行为（可触发 hooks 跑任意脚本）；放行 `cat *` 等于放行读任意文件（无路径维度）→ 该拦的拦不住 |
| P2 | **高频打扰** | 判定粒度是"整条命令"，与真实风险不对齐 | 安全的 `npm ci` 与危险的 `npm publish` 都被 `ask`；用户被训练成无脑点 y |
| P3 | **判定逻辑不可单点审阅** | 安全属性散在插件四模块 + hook 链 + `is_builtin_skill_script` 路径启发式 | 无法在一个地方回答"到底什么被允许" |
| P4 | **状态机过重** | 会话 TTL + 轮次 TTL + 惰性过期 + 轮次切换清空，叠加 6 个正交审批选项 | 用户要理解内部状态才能做安全决策；不同路径下"放行"语义漂移 |
| P5 | **接缝不稳定** | 预执行"预测"命令会不会越界 | `python -c …`、`curl \| sh` 预测不了；只能 fallback 到 ask，回到 P2 |

一句话：**现状把"资源约束"问题，错当成"字符串过滤"问题来解决。**
两者的错配同时造成 P1（漏）与 P2（扰）。

## 2. 借鉴 Apple App Sandbox：四条原则 → shell 的翻译

| Apple | 本模式的映射 |
|---|---|
| **声明式 entitlements**：启动即定，进程生命周期内不可变 | **能力包络（Capability Envelope）**：一个 shell 会话启动即定，profile 声明它可触碰的资源 |
| **默认拒绝 + 容器**：容器外一律不可写 | profile 默认 deny；写集合是加法白名单（`deny default` + 精确 allow） |
| **内核强制，不依赖应用自查** | Seatbelt 强制；判定**不看命令字符串**，只信内核裁决 |
| **越过容器需用户把能力交出去**（powerbox / security-scoped bookmark） | 越界 = **能力申请**；用户授予一条 **scoped token**（资源 × 时效），而非豁免一条命令 |
| **系统级 entitlement 不可自助获取** | **不可授予能力**（凭证读、系统路径写、策略文件写、privilege）→ 永远 deny，不进审批面板 |
| **审查/声明是静态、可见的** | profile 是明文声明式文件，`cat` 出来即最终语义，可直接审阅 |

核心洞见：Apple 从不问"这个进程想调用哪个 API"，它问"这个进程**被授权了哪些资源**"。
本模式把同一个问题问给 shell。

## 3. 模式总览

不变式：**每个 shell 调用都在一个能力包络内运行；命令文本只喂给 shell 执行，不参与授权判定。**

```
run(命令) = 在 包络(profile) 内执行
权限判定对象 = 包络覆盖的资源集合（不是命令）
```

三档判定（对齐 Apple 的"容器内 / 容器外可申请 / 系统级不可得"）：

| 档 | 语义 | 触发条件 | 动作 |
|---|---|---|---|
| **P** `permitted` | 命令所需资源全在包络内 | 运行时命令只触碰了包络允许的资源 | 内核沙箱内直接执行，**免审批** |
| **G** `grantable` | 命令需要包络外资源 | 内核拒绝 + 命令显式申请该能力 | **能力授权审批**（见 §5） |
| **D** `prohibited` | 申请的是不可授予能力 | 命中能力类别黑名单（§6） | 拒绝，**不进审批面板** |

关键：**P 档是主路径，且不消耗用户注意力**——macOS 交互式使用下绝大多数命令不再弹审批。
这正是"效果好"的定义：安全边界由内核保证（不漏），审批只在真扩权时发生（不扰）。

## 4. 能力包络（entitlement schema）

### 4.1 声明结构

```jsonc
{
  "fs.read":  { "allow": "all", "deny": ["<凭证目录>", "<策略文件>"] },
  "fs.write": { "roots": ["<项目根>", "<会话目录>", "<TMPDIR>", "<用户 extraRoots>"] },
  "net":      { "egress": true },
  "proc":     { "exec": true, "signal": "self" },
  "privilege": false,          // sudo / setuid —— 不可授予
  "device.raw": false          // 块设备读写 —— 不可授予
}
```

逐项语义：

| 能力 | 默认 | 说明 |
|---|---|---|
| `fs.read` | 全开 − 敏感 deny | 读方向用**减法**（`allow file-read*` + 少量 deny） |
| `fs.write` | 项目根 + 会话目录 + TMPDIR + 用户 extraRoots | 写方向用**加法**（`deny default` 兜底 + 精确 allow） |
| `net.egress` | 交互开 / 定时关 | 断网档是"来源档位"，不是用户开关 |
| `proc.exec` | 开 | 解释器仍在包络内（策略随子进程继承），不是逃逸 |
| `privilege` / `device.raw` | 关 | 不可授予，Profile 里没有对应 allow |

> 读写方向为什么用相反策略：SBPL 中 deny 恒优先于 allow。写用加法可精确枚举；
> 读用减法才能兼顾工具链（dyld、解释器、git/npm 缓存）。

### 4.2 包络的三层来源（≈ provisioning profile + powerbox）

| 层 | 载体 | 谁能改 | 说明 |
|---|---|---|---|
| 基线 | 代码内硬编码 | 无人（模型不可见） | §4.1 的默认值，`deny default` 兜底 |
| 用户声明 | `~/.zero/profile.json`（CLI 管理） | 仅用户（模型不可写） | 长期 extraRoots、长期 net 策略 |
| 会话授权 | 运行期 token 表（内存 / 可选持久化） | 用户（经审批） | 越界能力授权，会话结束失效 |

**模型无法自助扩大包络**：`~/.zero/profile.json` 与 `shell_guard.json` 同处
读 deny 列表（`fs.read.deny`）；这正是原 P0-1"自举越狱"的堵点（§6）。

## 5. 审批即能力授权（核心简化点）

### 5.1 不做预测，做"拒绝后申请"

越界时的链路（**enforce → observe → escalate**，不 predict）：

```
1. 命令在包络内执行
2. 命令触碰包络外资源 → 内核拒绝 → 进程报 "Operation not permitted"
3. shell 工具回投结构化错误：{sandbox_denied: true, needed: {kind, target}}
4. 模型（或命令）提出能力申请：write_paths=[...] / network=true
5. 系统构造「能力授权请求」→ 用户审批
6. 批准 → token 入会话表 → 重试；拒绝 → fail-closed
```

第 3 步的 `needed` 是**具体能力**，不是命令原文；第 5 步审批的对象是
`(fs.write, /Users/Admin/other, session)` 这一条能力。

### 5.2 审批面板收敛为 4 项（沿"时效"一条轴）

| 选项 | 效果 | Apple 类比 |
|---|---|---|
| 本次允许 | 一次性 token，用后即焚 | powerbox 单次选择 |
| 本会话允许 | 会话级 token（内存），会话结束失效 | 内存中的 security-scoped bookmark |
| 总是允许 | 写入 `profile.json`（持久化，用户资产） | 持久化 bookmark |
| 拒绝 | 不执行 | — |

删除的两个选项及理由：

- **「总是允许（该命令）」**：命令不是能力，沉淀命令模式会把 P1 的假安全搬回来；
- **「本轮放行」**：与"本会话"只差一个粒度，收益不抵认知成本。

面板文案示例：

```
shell 请求能力：写入 /Users/Admin/other （当前包络：项目根 + 会话目录 + TMPDIR）
  [1] 本次允许   [2] 本会话允许   [3] 总是允许   [4] 拒绝
```

### 5.3 token 模型（security-scoped bookmark 类比）

```
token = (kind, target_realpath, expiry)
```

- 授予后可撤销；会话 token 存内存，永久 token 写 `profile.json`；
- 能力绑定的是"被指向的资源"，不是"被信任的进程"——同一个 `cp` 命令，
  授权写 A 目录不等于授权写 B 目录。

## 6. 不可授予集合（prohibited）

用**能力类别**黑名单，取代 `DEFAULT_DENY` 的 20 条命令 glob：

| 不可授予 | 归档手段 |
|---|---|
| 读凭证目录（`~/.ssh` `~/.aws` `~/.config/gh` `~/.netrc` Keychain） | `fs.read.deny`（内核 deny-read），申请也返回"不可授予" |
| 写系统路径（`/` `/System` `/usr` `/bin` `/Library` …） | 写集合不含这些根；申请 `fs.write /` → 不可授予 |
| 改写策略文件（`profile.json` `shell_guard.json` `scheduler/`） | `fs.read.deny` 且不在任何写集合 → 防自举越狱 |
| `privilege`（sudo / setuid） | profile 无 privilege allow；沙箱内 setuid 不可用 |
| `device.raw`（`dd of=/dev/*`、`mkfs`） | profile 无 raw device allow |

于是现状 `DEFAULT_DENY` 表达的危险全部被"**包络里根本没有这些能力**"覆盖：

- `rm -rf /` → 写集合不含 `/` → 内核逐路径拒绝；
- `dd of=/dev/sda` → 无 device.raw；
- `sudo …` → 无 privilege + 沙箱内 setuid 不可用；
- `cat ~/.ssh/id_rsa` → `fs.read.deny`。

> 唯一不被文件系统表达的危险是 **fork bomb / 资源耗尽**（DoS）。它不属于
> "访问控制"，应由进程资源上限（rlimit / 超时）另管，不混进本模式。

## 7. 无人值守渠道的收敛

现状有 scheduler、subagent、其他非 CLI 渠道三套特例文案 + origin 分支。新模型统一：

```
无人值守（无审批交互能力） → 能力申请一律 fail-closed
```

- 文案统一为："该能力申请在无人值守渠道无法受理，如需放行请让用户预置到 profile"；
- 断网、更严格档位由**来源**决定（定时任务：`net.egress=false`），是 profile 的取值，
  不是 handler 里的分支。

## 8. 判定流程与 handler 收敛

### 8.1 新判定顺序（`before_tool_call`）

```
1. tool_name != "shell"                → 放行（不干预）
2. kwargs 未携带包络外能力申请           → 放行（交给内核沙箱，P 档主路径）
3. 能力申请命中不可授予集合               → 拒绝（D 档，不进面板）
4. 能力申请可授予                      → 授权审批（G 档）
      交互渠道：4 选项面板
      无人值守：fail-closed（§7）
5. 批准                                → token 入会话表 → 放行
```

对比现状 handler（130 行）的删减：删除 `policy.evaluate` 调用、删除
`_session_allow` / `_turn_allow` 两套 TTL 表与 `_alive` / 轮次切换逻辑、
删除三段 origin 分支与三段 reason 文案、删除 `store.rules()` 读取。
handler 收敛到约 30 行，且**只依赖包络**。

### 8.2 状态：单一维度

| 现状 | 新模式 |
|---|---|
| 会话 TTL 表 + 轮次 TTL 表 + 惰性过期 + 轮次切换清空 | 会话 token 表（内存；会话结束即失效） |
| 放行语义因路径而异（命中规则 / 会话 / 轮次） | 一律 = "该能力在当前包络内" |

## 9. 落地示例

| 命令 | 现状行为 | 新模式行为 |
|---|---|---|
| `git status` | 命中 `git status*` → 免审批（靠 glob，`git *` 也会命中） | 只触碰项目根 → P 档 → 免审批（靠内核，与命令文本无关） |
| `npm install -g pkg` | `npm *` 未在允许表 → ask；用户需在 6 选项里抉择 | 写 `/usr/local/lib` 越界 → 内核拒绝 → 报 needed → "本会话允许 → /usr/local/lib" → 重试成功 |
| `rm -rf /` | 命中 `rm -rf /` → deny（纯文本匹配，`rm  -rf /` 多空格可绕过） | 写 `/` 不在包络 → 内核逐路径拒绝；申请 `/` → **不可授予** |
| `cat ~/.ssh/id_rsa` | 命中 `cat *` → **放行**（P1 假安全，实际泄漏凭证） | `fs.read.deny` → 内核拒绝；申请读凭证 → **不可授予** |
| `python -c "open('/etc/hosts','w')"` | 无法预测 → ask | 命令原文不参与判定；写越界 → 内核拒绝 → 具体能力申请 |

## 10. 与现状对比

| 维度 | 现状（glob 三态 + 插件） | 新模式（能力包络） |
|---|---|---|
| 判定对象 | 命令字符串 | 资源能力 |
| 配置面 | allow / deny 命令模式列表 | profile（资源声明） |
| 规则规模 | 20 内置 deny + 27 内置 allow + 用户 glob + skill 脚本启发式 | 一份 profile，**零 glob** |
| 审批选项 | 6（y/a/s/t/n/d）正交乱项 | 4（本次/本会话/总是/拒绝），单一时效轴 |
| 状态 | 会话 TTL + 轮次 TTL + 惰性过期 + 切换清空 | 会话 token 表（单一维度） |
| 强制点 | 应用层（可绕过） | 内核（不可绕过） |
| 效果 | 假安全（漏）+ 高频打扰（扰） | 内核保证不漏；只在真扩权时问一次 |
| 审阅成本 | 四模块 + hook 链 + 启发式 | 单文件 `sandbox.py` + 一个 profile |

## 11. 非 macOS 降级

- **无内核沙箱平台**：包络无法强制 → 本模式**不是安全边界**。两选一：
  - fail-closed（越界能力申请仍审批，但"包络内"不提供内核保证）；
  - 退化为应用层尽力而为的路径检查（`fs.write.roots` 静态匹配）。
- **Linux**：`bubblewrap` 提供同一 `plan()` 接缝，包络语义不变；
- **Windows**：AppContainer / Job Object 另议。

原则：**宁可不声明"沙箱内免审批"，也不假装有内核保证。**

## 12. 威胁模型

> shell 在 macOS 上受内核 Seatbelt 强制约束：可触碰的资源限于能力包络
> （项目根 + 会话目录 + TMPDIR + 用户声明的 extraRoots + 会话级授权）。
> 人工审批的对象从"命令"收敛为"能力（资源 × 时效）"。`read` / `write` / `edit`
> 受同一份包络约束。
>
> **仍未覆盖**：包络放行网络时的数据外泄、宿主进程内存中的凭证、
> 跨重启的自改源码、会话历史自改、fork bomb 类资源耗尽（另由 rlimit 管）。
> 需要对抗性隔离时使用虚拟机。

## 13. 迁移步骤

**阶段 A（macOS 主体）**

1. `zero/sandbox.py`：包络 schema + profile 渲染 + `sandbox-exec` 包裹 +
   能力探测（平台 / 二进制 / 自检探针）。
2. `zero/tools/shell.py`：包络内包裹执行 + 越界结构化错误（携带 `needed`）+
   `write_paths` / `network` 申请参数。
3. `plugins/shell_guard/`：**删除** `policy.py` 的 glob 引擎与内置规则表；
   handler 收敛为 §8.1 的 5 步；approval 面板收敛为 4 项；
   `store.py` 的 allow/deny 键 → `profile.json` 的资源键。
4. `read` / `write` / `edit`：接 `check_readable` / `check_writable` 硬边界。

**阶段 B**：`write` / `edit` 越界接同款能力授权；断网档细化。
**阶段 C**：Linux `bubblewrap` provider。

**测试清单**

- 包络渲染（读减法 / 写加法 / 不可授予项缺席）；
- 三档判定（P 免审批 / G 授权 / D 不进面板）；
- 能力授权（本次 / 本会话 / 总是 / 拒绝 / 无人值守 fail-closed）；
- 冒烟：`sandbox-exec -f <profile> cat ~/.ssh/id_rsa` 必须 `Operation not permitted`。
