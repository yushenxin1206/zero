# Shell 沙箱方案（macOS Seatbelt 优先）——v2

> 承接 `docs/shell_permission.md`（v4：通用 Hooks + shell_guard 三态判定）。
> 本文只做一件事：把 shell 的"执行方式"从**人工审批**升级为**内核级约束**，
> 让沙箱内的命令免审批、沙箱外的命令走现有审批。
> 与 shell_permission.md 的分工：那份文档管"要不要跑"，本文管"跑起来能碰到什么"。

> **现状（v3，已实现——取代下文的五档模型与「shell_guard 改造」节）**
>
> - 档位收敛为**三档**：**P 包络内（免审批）/ G 可授予（审批）/ D 不可授予（永不进面板）**。
>   S0/S1/S1n/S2/SX 五档、以及"unsandbox + 三态逻辑保留"的过渡形态作废。
> - 判定**看资源不看命令文本**：glob 规则引擎、内置 deny/allow 规则表、
>   `is_builtin_skill_script` 启发式全部删除。shell 命令默认落在 P 档；
>   D 档只在"能力申请携带了不可授予资源"时判定。
> - 实现形态：`zero/sandbox.py`（包络 / profile 渲染 / `wrap` / 双向探针 /
>   `check_readable` / `check_writable` / CLI）+ `zero/sandbox_guard.py`
>   （`before_tool_call` handler + 审批分发）。`plugins/` 整目录已删除。
> - 配置文件：`~/.zero/profile.json`（键 `extraRoots` / `unsandbox`）。
>   `shell_guard.json` 弃用；**现有 glob 规则无法自动迁移**，需手工
>   `zero-sandbox add-root <path>` 重建。
> - CLI：`zero-sandbox check | list | add-root | add-unsandbox`（原 `zero-guard` 改名）。
> - 审批面板按键：扩权 = `s` 本会话允许 / `a` 总是允许 / `n` 拒绝（3 键）；
>   脱离沙箱与「沙箱不可用」降级 = `y` 允许一次 / `n` 拒绝（2 键）。
>   （原 6 键的「本轮」「总是拒绝」已删除——它们正是 TTL / 轮次状态机的来源。
>   扩权不给「本次」：只批一次必然引入调用级槽位，等于把那套状态机再请回来。）
> - **检查已收敛为单源：`zero/sandbox_guard.py`**：
>   - `read`/`write`/`edit` 越界走审批门，由守卫在**工具执行前**裁定。
>     读方向是减法（全开减不可授予）→ **只有 D 档，永不进面板**；写方向 →
>     D 档拒绝 / G 档审批（`s`/`a`/`n`），授权落会话扩权表。
>   - **工具入口不再有 check**（早期版本曾加过，现已移除）。原因：它调用的是
>     **同一个谓词、同样的入参、在同一个进程里**——守卫放行则 check 必过，
>     守卫拒绝则工具不跑，**不构成独立的第二道墙**，只是把同一判断做两遍；
>     而重复实现同一策略已经实测漂移过一次（探针标记：read 工具允许 / shell 拒绝）。
>   - **fail-closed 由守卫自理**：hook 契约本身是 **fail-open**（handler 异常只
>     记日志、视为不干预），所以守卫在 `__call__` 里包一层 try/except，异常时
>     返回**明确拒绝**——否则守卫自身的 bug 就等于"静默放行"。
>   - **覆盖由 `HANDLED_TOOLS` + 回归测试锁住**：新增带 `*_path` 参数的工具若
>     没登记，`tests/test_sandbox_guard.py::test_guard_covers_all_file_tools`
>     会变红。
>   - 无人值守渠道下，文件类工具的 G 档与 shell 一样 fail-closed。
>   - **仍存在的依赖**：边界现在依赖"守卫被装上"。`core/loop.py` 与
>     `core/runner.py` 都是 `hooks or Hooks()`——不传 hooks 得到的是空基类而非
>     `HookChain`，守卫不在链上。生产路径（`main.py::build_hooks`）一定会装。
> - mach 面：**不裸放 `mach-lookup`**，并显式 deny `com.apple.SecurityServer` /
>   `com.apple.securityd`。实测（本机 macOS）：裸放会让沙箱内进程连上 securityd，
>   一旦如此，"deny 的钥匙串文件（`~/Library/Keychains`）"形同虚设——只挡文件、
>   不挡服务。去掉裸放后 `security list-keychains` 由 rc=0 变 rc=1（服务不可达），
>   而 git / curl / clang / make / python3 / pydantic 全部逐项不变。
> - **已知残留①：launchd 域可达（只能读，不能变）**
>   - 读：`launchctl print gui/<uid>` 沙箱内 rc=0，输出 **3261 行**、其中 **15 行**
>     含疑似密钥字样（TOKEN / SECRET / PASSWORD / API_KEY / CREDENTIAL）。
>     **只统计了数量，未核对是否为可用密钥值** → 泄露面存在、规模需人工 triage。
>     它暴露的是"本机装了/在跑什么"：备份、同步、VPN、开发工具及其参数与启动环境。
>   - 变：`bootstrap`（用我们写的 plist）/ `submit` 在沙箱内被拒；对照实验里同一
>     命令在沙箱外 rc=0 且 launchd 派生的进程**真的写出了包络外文件**，即该向量
>     本身真实存在。**写需要 privileged bootstrap right，读走继承的 bootstrap
>     port**——这是语义区分，不是随机运气。
>   - **关不掉**：`(deny mach-lookup …)` 对 launchd 的 `global-name` 精确名与
>     `global-name-prefix` 两种写法**均无效**（print 照样 rc=0），因为它不经
>     `mach-lookup` 可达。同一个 deny 对具名服务是有效的（securityd ✓、cfprefsd ✓），
>     所以别再试 deny launchd。
>   - 报警线：`tests/test_sandbox_guard.py::test_launchd_mutation_blocked` 锁住
>     "读允许 / 写拒绝"。macOS 若翻转该语义，测试立刻变红——否则我们会静默失去
>     这道墙。
> - **已知残留②：`(import "system.sb")` 基线比威胁模型宽**（launchd 可达即证据）。
>   彻底解决需用手工最小基线替掉该 import，按 macOS 版本反复实测。
> - 显式记录的旁路（不受控）：`utils/notify.py` 的 `Popen`；agent 自身进程
>   （`maintenance/`、会话落盘、provider HTTPS）。

## 修订记录（v1 → v2）

| 变更 | 内容 |
|---|---|
| 实现形态 | `tools/sandbox/` 五模块子包 → **单文件 `zero/sandbox.py`**（约 250 行）+ 一个测试文件 |
| 配置 | 取消 `sandbox.json`，`unsandbox` / `extraRoots` 并入现有 `shell_guard.json`，**零新配置文件** |
| session_dir 载体 | HookContext 新增字段 → **ToolContext 顶层字段**，`hooks.py` / `runner.py` 零改动 |
| profile 传递 | `-D` 参数化 + `run/` 缓存 → **字面量插值 + 一次性临时文件**（无缓存、无注入面） |
| TMPDIR | 取消重定向，仅放行 `DARWIN_TMPDIR` 写 |
| S2 扩权 | 从阶段 B 提前到**阶段 A**：`write_paths` 参数 + 扩权审批，"批准 = 放宽沙箱"首批落地 |
| 自检探针 | 不再依赖机器上既有文件（fresh install 假阳性）→ 宿主预置探针文件，新增读基线探针 |
| 已知限制 | 补会话历史自改残余风险 |

## 0. 决策记录（本方案已定稿的取舍）

| 议题 | 决定 | 理由 |
|---|---|---|
| 人工批准后的"未沙箱"语义 | **批准 = 放宽沙箱写集合**，内核沙箱始终在线 | 一个 y 撤掉全部 OS 隔离太陡；扩权让每次批准的影响面可枚举 |
| 扩权触发方式 | 沙箱拒绝后，模型**重试时显式声明 `write_paths` 参数** → 用户审批 → 会话级扩权集（TTL 1h） | 预执行无法可靠预测命令的越界需求；把"声明意图"交给模型、"批准"交给用户，是最诚实的接缝 |
| 完全脱离沙箱是否允许 | macOS 上**仅**命中用户预置的 `unsandbox` 白名单才允许，且强制审批 | 保留 `sudo` / `brew` 这类系统级操作的口子，但钥匙只在用户手里 |
| 路径策略覆盖范围 | **同时约束 shell 与 read / write / edit 工具** | 否则 `write` 工具可以直写 `~/.zero/shell_guard.json`、`~/.zshrc`，沙箱形同虚设 |
| 无人值守渠道 | subagent → S1（读写 cwd + 网络）；scheduler → S1 断网档 | 子 agent 从"完全不能执行"变成"能干活且被关住"；定时任务是无人值守 + 可重复触发，收紧一档 |
| 是否支持管道 | **不引入 `/bin/sh -c`**，保持既有无 shell 语义 | `core/context.py` 的 SHELL_NOTE 已明确声明不支持 `\|` `&&` `>`；改了会与提示词漂移 |
| 档位数量 | 落地 S1 / S1n / S2 / SX，S0（只读档）留二期 | 不做档位套娃，先把读写档做正确 |
| 实现形态 | **单文件 `zero/sandbox.py`**，不建子包 | 无 UI、无独立存储；codebase 风格是单文件中型模块（context.py / shell.py）；"唯一真相源"在单文件内物理成立 |
| 配置文件 | **零新增**，扩权根并入 `shell_guard.json` | 同一守卫域、同一 CLI、同一个模型不可写的文件，用户心智只有一个 |

## 1. 现状与必须堵的窟窿

现有链路：`core/runner.py` → `hooks.before_tool_call` → `shell_guard` 三态
（deny > allow > ask）→ `ask` 时 CLI 就地审批，无人值守 fail-closed。

三个问题按严重度排列，都会让本方案"看起来有隔离、实际没有"：

**P0-1 `~/.zero` 可写 = 自举越狱。**
`~/.zero/shell_guard.json` 是审批规则的落盘位置（`plugins/shell_guard/store.py`）。
而 `plugins/shell_guard/handler.py` 中 `tier == "allow"` 是在审批之前直接
`return None` 的。沙箱内一条命令写入 `{"allow": ["*"]}` 即可让后续任意命令
命中白名单免审批——整条策略链一次性作废。

**P0-2 沙箱只覆盖 shell，`write` 工具零约束。**
`tools/write.py` / `tools/read.py` 直接 `open()` 任意路径，且
`handler.py` 对非 shell 工具是 `return None` 直接放行。模型写
`~/Library/LaunchAgents/*.plist`、`~/.zshrc` 根本不用经过 shell。

**P0-3 未沙箱档不得被 allow 规则豁免。**
未沙箱 = 无任何 OS 约束。若它仍能被 `git *`、`npm *` 这类宽规则命中免审批，
规则就变成了任意执行的快捷通道。**未沙箱路径必须强制人工审批。**

## 2. 档位模型与判定流程

### 2.1 档位

| 档位 | 写集合 | 网络 | 进入方式 | 审批 |
|---|---|---|---|---|
| **S0** 只读沙箱（二期） | 仅 TMPDIR | 否 | 命令命中只读白名单（`ls` / `git status` / `cat`…） | 免 |
| **S1** 读写沙箱（默认） | 项目根 + 当前会话目录 + TMPDIR | 放行 | macOS 且沙箱可用 | **免** |
| **S1n** 断网读写沙箱 | 同 S1 | 禁止 | scheduler 来源 | **免** |
| **S2** 扩权沙箱 | S1 + 会话级追加路径（TTL 1h） | 同来源档位 | 命令显式携带 `write_paths`，经用户审批 | 首次审批，之后免 |
| **SX** 脱离沙箱 | 无限制 | 放行 | **仅**用户预置 `unsandbox` 白名单命中 | **强制审批** |

S1 / S1n / S2 都跑在 `sandbox-exec` 下，区别只是 profile 的写集合与网络段。
SX 是唯一无沙箱路径，且不可由模型自助获得。

### 2.2 判定流程（`before_tool_call` 内，顺序即优先级）

```
1. 命中 deny 规则                          → 拒绝（不变）
2. 命中 unsandbox 白名单（用户预置）         → 强制审批 y/n，批准后 SX；无「总是允许」
3. 沙箱不可用（非 macOS / 探针失败 / 根过宽） → 现有三态 allow / ask +
                                             无人值守 fail-closed（与今天一致）
4. kwargs.write_paths 非空                   → 交互渠道：扩权审批
                                             （批准 = 会话级追加这些写路径，TTL 1h）
                                             无人值守 fail-closed 引导 add-root
5. 其余                                     → S1 免审批（scheduler 来源为 S1n 断网档）
```

第 5 步是新增的主路径，也是本方案的收益点：**macOS 交互式使用下，
绝大多数命令不再弹审批**。第 4 步是"批准 = 放宽沙箱"的落地——审批对象是
**路径**而非命令，批准后命令仍跑在沙箱内，只是写集合多了被批准的路径。
第 3 步刻意保持与今天逐字节一致，非 macOS 平台零回归。

### 2.3 项目根过宽时的拒绝

`core/context.py::get_cwd()` 返回的是 `Path.cwd()`——即用户启动 `zero` 的目录。
评估根必须显式限定，否则等于没有沙箱：

- 项目根 ∈ {`/`、`$HOME`、`$HOME` 的上级} → **判定沙箱不可用**，退回第 3 步审批；
- 项目根为符号链接 → 取 `realpath` 后再判定与写入。

## 3. 权限矩阵（S1 基线）

| 路径 | 读 | 写 | 说明 |
|---|---|---|---|
| 项目根（`get_cwd()` 的 realpath） | ✅ | ✅ | 唯一由模型日常读写的区域 |
| 当前会话目录 `~/.zero/sessions/{ch}/{date}/{sid}` | ✅ | ✅ | plan skill 的 `plan.md` / `plan.json` 落在这里 |
| 其他会话目录 | ✅ | ❌ | 可读（session-history skill），不可篡改 |
| `DARWIN_TMPDIR` + `/private/tmp` | ✅ | ✅ | 见 §5.3 |
| `/usr` `/System` `/Library` `/bin` `/sbin` `/opt` | ✅ | ❌ | dyld、解释器、工具链 |
| `~/.gitconfig` `~/.npmrc` `~/.cache` `~/Library/Caches` | ✅ | 缓存目录可写 | 缺读会让 git/npm 行为异常 |
| `~/.zero/config.json` | ❌ | ❌ | 含 provider api_key |
| `~/.zero/shell_guard.json` | ❌ | ❌ | P0-1 |
| `~/.zero/scheduler/` | ❌ | ❌ | 防伪造定时任务 |
| `~/.ssh` `~/.aws` `~/.config/gh` `~/.netrc` `~/Library/Keychains` | ❌ | ❌ | 凭证，S1 网络放行时是主要外泄面 |
| 其他 | ❌ | ❌ | `deny default` 兜底（含 `~/.zero/profiles/`，无需显式规则） |

> `~/.zero/memory.md` 由 `maintenance/memory.py` 在 agent 进程内重写，
> 不走 shell、也不走 `write` 工具，因此**不需要**写权限。

## 4. 架构与改动清单

### 4.1 单文件模块：`zero/sandbox.py`

sandbox 是被 tools 和 plugins **共用**的横切设施，不是 tool；放顶层
（先例：`config.py`），避免 `plugins/shell_guard` 反向 import `tools/` 子包。
无 UI、无独立存储，不拆子包。内部按注释分段，职责如下：

```
zero/sandbox.py
├── 能力探测   available(project_root)   平台 + sandbox-exec 存在性 + 自检探针
│                                        （惰性：首个沙箱命令前触发，进程内缓存）
├── scope 计算 write_set(...) / deny_read_paths(...)   纯函数
├── profile 渲染 render(scope, network) -> str        纯函数、字面量插值
├── argv 包裹   wrap(argv, profile_text)               mkstemp 落盘 + 前置 sandbox-exec
├── 探针       _probe(...)                            §8 的五条自检
└── 会话扩权集  grant_session() / session_extra_roots()  会话 id 键控 + TTL 惰性过期
```

对外 API 仅四个：`available()` / `plan(argv, working_dir, ctx)`（shell 用）、
`check_readable(path)` / `check_writable(path)`（read / write / edit 用）。
`shell.py` 的包裹判断与 handler 的免审批判断都收敛到这同一组函数——
"唯一真相源"在单文件内物理成立，不会出现"hook 认为在沙箱内、
shell 却真的脱离了"的漂移。

### 4.2 数据载体：ToolContext.session_dir

hook 侧与 tool 侧都要算写集合，而会话目录是共享数据。**放 ToolContext 顶层字段**
（`models.py`，类型化顶层字段本就是 ToolContext 的既定设计）：

```python
class ToolContext(Base):
    ...
    session_dir: str = ""    # 新增；默认空，向后兼容
```

- hook 侧：`ctx.tool_context.session_dir`——runner 构建 HookContext 时本就
  **按引用复用** tool_context（`runner.py::_hook_context`），零改动；
- tool 侧：`shell.run(context)` 直接 `context.session_dir`；
- 填充点：`core/loop.py` 的 ToolContext 构造处 +1 行；
  `context.py` 的两处兜底构造用默认值，不动。

`hooks.py` 契约、`runner.py`、`bus/`、`channels/` 全部零改动。
`ToolCallDecision` 仍只有 `proceed / reason / result`——它表达不了
"改变执行方式"，这个职责本来就该在 shell 工具一侧，hook 只保留判定。

### 4.3 改动清单（v2）

```
zero/sandbox.py                        新增（单文件，见 §4.1）
tests/test_sandbox.py                  新增
zero/models.py                         +1 字段 ToolContext.session_dir
zero/core/loop.py                       ToolContext 构造点 +1 行
zero/tools/shell.py                     包裹 + write_paths 参数 + sandbox_denied 标注
zero/tools/write.py / read.py / edit.py 接 check_writable / check_readable 硬边界
zero/plugins/shell_guard/handler.py     §2.2 判定顺序 + write_paths 扩权审批
zero/plugins/shell_guard/approval.py    request() 增加 options 参数（面板复用）
zero/plugins/shell_guard/store.py      +unsandbox / extraRoots 两个键
zero/plugins/shell_guard/manage.py     sandbox 子命令
zero/core/context.py                   SHELL_NOTE 补一句
docs/shell_permission.md               第 7 节威胁模型修正 + 交叉引用本文
```

## 5. Seatbelt profile 设计

### 5.1 SBPL 的硬语义

1. **deny 优先**：冲突时 deny 一定压过 allow。因此**写权限用纯加法**
   （`deny default` 兜底 + 精确 allow 白名单），**读权限才用减法**
   （`allow file-read*` 兜底 + 少量 deny）。混用会导致"想开的洞被 deny 吃掉"。
2. **只认真实路径**：规则匹配 realpath，`~` 不展开、软链不解析。
   所有路径在 Python 侧预先 `realpath(expanduser(p))`。
3. **字面量插值，不用 `-D` 参数**：`-D` 的两大理由（argv 长度上限、注入）
   在 `-f` 文件方案下均不成立。render() 把 realpath 后的路径直接写进
   SBPL 字符串，仅转义 `\` 与 `"`；转义函数遇到无法安全处理的输入直接抛错
   （fail-closed）。好处：cat 出来的 profile 即最终语义，可直接审计，
   无需脑内代换参数。

### 5.2 模板（render() 的输出；`<…>` 为 Python 侧填充的字面量）

```scheme
(version 1)
(deny default)

; 基线：dyld、系统 mach 服务等。需在目标 macOS 版本上实测，
; 若该 import 不可用，改为手工最小基线（file-read* 系统路径 + mach-lookup）。
(import "system.sb")

(allow process-fork)
(allow process-exec)
(allow signal (target self))
(allow sysctl-read)
(allow mach-lookup)

; ---- 读：全开，再抠掉凭证与策略文件（字面量均 realpath）----
(allow file-read*)
(deny file-read*
  (subpath "<HOME>/.ssh")
  (subpath "<HOME>/.aws")
  (subpath "<HOME>/.config/gh")
  (literal "<HOME>/.netrc")
  (subpath "<HOME>/Library/Keychains")
  (literal "<ZERO_ROOT>/config.json")
  (literal "<ZERO_ROOT>/shell_guard.json")
  (subpath "<ZERO_ROOT>/scheduler")
  (literal "<ZERO_ROOT>/.sandbox-probe-r"))     ; 探针标记，恒定渲染（见 §8）

; ---- 写：纯加法白名单，其余由 deny default 兜底 ----
(allow file-write*
  (subpath "<PROJECT_ROOT>")
  (subpath "<SESSION_DIR>")
  (subpath "<DARWIN_TMPDIR>")
  (subpath "/private/tmp")
  (subpath "<EXTRA_ROOTS…>")                    ; shell_guard.json 的 extraRoots
  (subpath "<会话扩权集…>"))                     ; S2：write_paths 审批后追加

; ---- 网络：S1 放行 / S1n 整段省略 ----
(allow network*)
```

> `(literal "…")` 若在目标版本报错，退化为对其父目录整体 `(subpath …)` deny
> ——不违反 §5.1 的加法规则，因为读方向本就是减法。

### 5.3 TMPDIR：不重定向

macOS 的 `TMPDIR` 是 `/var/folders/xx/…/T`，不在项目根内，不处理会让
npm / pip / cargo / 编译器大面积报错。**只需 profile 放行
`getconf DARWIN_USER_TEMP_DIR` 与 `/private/tmp` 写**，子进程环境保持原样。
不做"重定向 TMPDIR 到项目根"——那只会让 repo 里多一个需要 gitignore 的
目录（`.zero-tmp`），没有额外收益。

### 5.4 包裹方式与 profile 生命周期

```python
def wrap(argv: list[str], profile_text: str) -> tuple[list[str], str]:
    # profiles 目录 0700；不在写集合内 → 沙箱内进程改不了它（deny default 兜底）
    fd, path = tempfile.mkstemp(suffix=".sb", dir=PROFILES_DIR)
    with os.fdopen(fd, "w") as f:
        f.write(profile_text)
    return ["/usr/bin/sandbox-exec", "-f", path, *argv], path   # 不加 /bin/sh -c
```

`shell.py` 的改动（保持无 shell 语义不变）：

```python
argv = shlex.split(command)
plan = sandbox.plan(argv, working_dir=working_dir, ctx=context)
if plan.run_sandboxed:
    argv, profile_path = sandbox.wrap(argv, plan.profile_text)
proc = await asyncio.create_subprocess_exec(*argv, ...)
stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
...
os.unlink(profile_path)     # 进程结束后删除；存活期 = 进程生命周期
```

**不缓存 profile**。40 行 profile 落盘是微秒级 IO，命令本身至少几十毫秒；
缓存却引入三样负资产：key 失效管理（S2 扩权即变更写集合）、`run/` 常驻目录、
以及"缓存 profile 被沙箱内进程改写以弱化下次执行"的攻击面（v1 为此还要
追加 deny 规则，属于为不必要的优化付安全税）。一次性文件 + 用完即删，
三个问题一起消失。unlink 必须在 `communicate()` 之后：create_subprocess_exec
返回时 sandbox-exec 刚完成 exec，可能尚未读完 profile 文件。

## 6. shell_guard 改造

### 6.1 判定顺序（§2.2 的落地）

```python
if tool_name != "shell":
    return None

verdict = policy.evaluate(command, allow_rules, deny_rules)
if verdict.tier == "deny":
    return ToolCallDecision(proceed=False, reason=…)

# 1. unsandbox 白名单 → 强制审批 y/n（无「总是允许」）
if policy.matches(command, unsandbox_rules):
    ok = await approval.request(..., options=YN_ONLY)
    return None if ok else ToolCallDecision(proceed=False, reason="用户拒绝脱离沙箱")

# 2. 沙箱不可用 → 现有三态逻辑原样保留（含无人值守 fail-closed）
if not sandbox.available(project_root):
    ...现有 allow / ask 分支不动...

# 3. write_paths 扩权审批（S2）
write_paths = kwargs.get("write_paths") or []
if write_paths:
    # scheduler / subagent / 非 CLI 渠道：fail-closed，引导 add-root
    ok = await approval.request(..., options=EXPAND_PANEL, paths=write_paths)
    if ok:
        sandbox.grant_session(ctx.session_id, write_paths)   # TTL 1h
        return None
    return ToolCallDecision(proceed=False, reason="用户拒绝扩权")

# 4. 沙箱内免审批（S1 / S1n）
return None
```

扩权集存于 sandbox 模块内（会话 id 键控 + TTL 惰性过期，
与 ShellGuard 的 `_session_allow` 同款模式）；handler 批准后写入，
`plan()` 渲染 profile 时读取。

### 6.2 shell 工具新参数 `write_paths`

```json
"write_paths": {
  "type": "array",
  "items": {"type": "string"},
  "description": "沙箱写集合之外的额外可写路径。提供后将触发用户审批；无人值守渠道直接拒绝。"
}
```

配合 §10 的 `sandbox_denied` 错误文案：命令因越界写入失败后，
错误信息明确告知模型"重试时带上 write_paths"，而不是盲目换命令重试。

### 6.3 审批面板收敛

`approval.request()` 增加 `options` 参数（默认现值，面板协议本就携带
options，channel 端零改动）。两种新面板均只有两个选项：

- **unsandbox 面板**：允许（本次）/ 拒绝——`a`/`s`/`t` 对脱离沙箱无意义；
- **扩权面板**：本会话允许这些路径 / 拒绝——批准影响面 = 所声明路径 × TTL 1h。

原 ask 档在 macOS 沙箱可用时不再触发（§2.2 第 3 步兜底）。
`DEFAULT_ALLOW` 的只读命令清单保留：用于非 macOS 路径与二期 S0 档。

## 7. 路径策略：read / write / edit

同一份 scope 判定（`sandbox.check_readable` / `check_writable`）供两个
消费者使用，语义有**故意的差异**：

| 消费者 | 读 | 写 |
|---|---|---|
| shell（Seatbelt） | 全开 − 敏感 deny | 项目根 + 会话目录 + TMPDIR + 扩权集 |
| `read` / `write` / `edit`（应用层） | 全开 − 敏感 deny | 项目根 + 会话目录 + 扩权集 − 会话目录以外的 `~/.zero` |

- **阶段 A（本次）**：应用层越界写入直接 **fail-closed**，返回结构化错误
  （`{"success": false, "error": "sandbox_scope: 路径不在可写范围内", "hint": "…"}`
  + 引导 `zero-guard sandbox add-root <path>`）。最小实现，先止血。
- **阶段 B**：把越界写入接到与 shell 同款的 `write_paths` 审批
  （`approval.request` 现成，批准即入会话扩权集），语义与 S2 完全一致。

> 应用层差异说明：`write` 工具能在项目根内自由写（含 `.git/hooks`），
> 这是用户自己的仓库，不额外收紧；但它**不能**写 `~/.zero` 下除会话目录
> 以外的任何东西——P0-1 就是这么堵住的。

## 8. 失败与降级（fail-closed）

三条降级路径，全部指向"退回现有审批"，**绝不静默无沙箱执行**：

1. **平台**：非 macOS（`sys.platform != "darwin"`）→ 沙箱不可用。
2. **能力**：`/usr/bin/sandbox-exec` 缺失（含未来被 Apple 移除）→ 不可用。
   `sandbox-exec` 自 10.13 起被标 deprecated 但至今无替代品，所以必须显式探测
   而不是假设存在。
3. **自检探针不符**：惰性执行（首个沙箱命令前触发，进程内缓存，
   按项目根键控），任一条不符即判定不可用。

探针设计的关键：**探针文件由宿主（非沙箱进程）预先创建**，
不依赖用户机器上任何既有文件——全新安装（`~/.zero` 尚空）也成立。
v1 依赖 `config.json` 存在的读探测在 fresh install 上会假阳性。

| # | 操作 | 期望 | 验证什么 |
|---|---|---|---|
| 1 | 宿主创建 `~/.zero/.sandbox-probe-r` 并写入标记 | 成功（宿主不受限） | 前置条件 |
| 2 | 沙箱内 `cat ~/.zero/.sandbox-probe-r` | **EPERM**（非成功、非 ENOENT） | 读 deny 生效 |
| 3 | 沙箱内 `touch ~/.zero/.sandbox-probe-w` | 失败 | `~/.zero` 写 deny 生效 |
| 4 | 沙箱内 `touch <项目根>/.sandbox-probe-w` | 成功 | 写 allow 生效（防收得过紧） |
| 5 | 沙箱内 `cat /bin/ls` | 成功 | 读基线生效（防 system.sb 基线失效导致全线 EPERM） |

探针路径 `.sandbox-probe-r` **恒定渲染进每个 profile 的 deny-read 列表**
（§5.2 模板可见），保证探测的就是真实执行环境。探针文件用完即删；
第 3 步若意外成功（沙箱失效），宿主负责清理误创建的文件并降级。
用户可用 `zero-guard sandbox check` 查看结论与原因。

这条把"profile 写错 = 静默拥有全权限"变成 fail-closed，
是本方案最重要的安全属性。

## 9. 配置与 CLI：零新文件

`~/.zero/shell_guard.json` 扩展两个键（store.py 的键集合从
`{"allow", "deny"}` 扩为四键；load 对缺键回空，旧文件自动兼容）：

```json
{
  "allow": ["git fetch*"],
  "deny": [],
  "unsandbox": ["brew *"],
  "extraRoots": ["/Users/Admin/other-repo"]
}
```

- `unsandbox`：脱离沙箱白名单（命中仍强制审批 y/n）；
- `extraRoots`：长期可写根（用户手动维护）。

**没有 `enabled` / `network` 开关**：能力探测（平台 + 二进制 + 探针）就是开关，
留用户开关只会制造"开了但实际降级了"的解释成本；断网与否由来源档位
（S1n）决定，不是配置。整个文件仍在沙箱读写 deny 列表内：
**模型无法自助加白名单、加写根或关闭沙箱。**

CLI 扩展（`zero-guard`）：

```
zero-guard add unsandbox "brew *"        预置脱离沙箱白名单（强制审批）
zero-guard sandbox check                 打印能力探测 + 探针结论与降级原因
zero-guard sandbox add-root ~/other-repo 追加长期可写根（写入 extraRoots）
zero-guard sandbox list                  当前档位、写集合、网络开关、扩权集
```

## 10. 结果回投

沙箱拒绝时不能只把 stderr 原文丢回去。模型会把 `Operation not permitted`
当成"路由问题"并换命令重试，甚至去试探 `docker.sock`。`shell.py` 需显式标注：

```json
{
  "returncode": 1,
  "stdout": "",
  "stderr": "touch: …: Operation not permitted",
  "sandbox_denied": true,
  "error": "sandbox_scope: 写入 /Users/Admin/other 被沙箱拒绝（当前可写：<项目根>、<会话目录>、TMPDIR）。交互渠道可在重试时提供 write_paths=[\"/Users/Admin/other\"] 触发扩权审批；或由用户执行 zero-guard sandbox add-root。不要尝试绕过。"
}
```

判定用 `rc != 0` 且 stderr 匹配 EPERM 特征的**启发式**（程序自身也可能打印这句），
文案里保留"启发式提示"的措辞，避免误导模型。`core/context.py` 的
SHELL_NOTE 同步补一句：macOS 下 shell 默认在沙箱内执行，被拒时带
`write_paths` 重试或求助用户，不要换命令绕过。

## 11. 已知限制与残余风险

- **网络放行下防不了外泄**。S1 读全开 + 网络放行，凭证目录已 deny-read，但
  项目根内的 `.env`、源码仍可被外发。S1 的目标是"防手滑 + 限制破坏半径"，
  不是"防提示注入外泄"。需要收敛时用 S1n（scheduler 来源即是）。
- **loopback 未隔离**。S1 放行网络含 127.0.0.1，可访问本机其他服务
  （Docker TCP 暴露时即为逃逸面）。`/var/run/docker.sock` 已被文件 deny 覆盖。
  收紧选项（默认关闭，二期）：deny loopback 段。
- **会话目录可写 = 历史自改**。沙箱内命令可改写当前会话的 `messages.jsonl`，
  注入伪历史影响下个进程。plan skill 需要该目录可写，收不了；威胁模型
  （防手滑）下可接受，在此明确记录。
- **write_paths 依赖模型自觉申报**。被注入的模型可以直接申报超大的扩权
  集合——但审批权在用户手里，单次批准的影响面 = 所声明路径 × TTL 1h，
  内核约束始终在线，这是与"一个 y 全放行"的本质区别。
- **`mach-lookup` 先粗后细**。v1 用宽放行保证兼容，后续可换成
  `(allow mach-lookup (global-name …))` 白名单。
- **`process-exec` 即解释器逃逸面**。`python -c` 属于被允许的执行，但仍在
  沙箱内（策略由子进程继承），不是逃逸；真正的逃逸只有 SX 白名单与
  `unsandbox` 规则被写坏两条路，都已收紧。
- **`sudo` / setuid 在沙箱内不可用**。这是 SX 白名单存在的唯一理由。
- **无 overlay / COW**。越界写入是被拒绝，不是被记录后回滚。要"可回滚"只能上
  虚拟机（Lima / Tart / Apple `container`），不在本方案范围。
- **跨重启自改**。项目根若包含 zero 自身源码，沙箱内的命令可以改
  `tools/shell.py`，下次启动即无沙箱。属用户自己的仓库，接受；
  若项目根就是 zero 源码目录，建议默认档更严（不给 extraRoots）。
- **DNS 依赖 `system.sb` 基线**。若解析失败，补
  `(allow mach-lookup (global-name "com.apple.mDNSResponder"))`
  与 `(allow network-outbound (remote unix-socket (path "/var/run/mDNSResponder")))`。

## 12. 实施步骤

**阶段 A：沙箱生效 + 扩权审批（本方案主体，全部核心语义落地）**

1. `zero/sandbox.py`：render / scope / wrap / available（含探针）/ 会话扩权集，
   配套 `tests/test_sandbox.py`（字面量转义、写白名单、读 deny、
   根过宽判定、realpath、S1/S1n 差异）。
2. `models.py` + `core/loop.py`：`ToolContext.session_dir`。
3. `tools/shell.py`：包裹 + `write_paths` 参数 + `sandbox_denied` 标注。
4. `read` / `write` / `edit`：接 scope 硬边界（fail-closed）。
5. `shell_guard/handler.py`：§6.1 判定顺序 + 扩权审批分支。
6. `shell_guard/approval.py`：options 参数化。
7. `shell_guard/store.py` + `manage.py`：unsandbox / extraRoots / sandbox 子命令。
8. `core/context.py` SHELL_NOTE 与 `docs/shell_permission.md` 第 7 节同步。

**阶段 B**：`write` / `edit` 越界接同款 `write_paths` 审批、S0 只读档、
loopback 收紧选项。

**阶段 C**：Linux `bubblewrap` provider（同一 `available()` / `plan()` 接缝；
Windows 用 AppContainer / Job Object 另议）。

**测试清单**

- `tests/test_sandbox.py`：profile 渲染（转义 / 白名单 / deny / S1n 差异）、
  scope（根过宽 / realpath / 会话目录）、wrap、探针（macOS 真跑，其他平台 skip）
- `tests/test_shell_guard.py` 扩展：沙箱内免审批、deny 优先、
  write_paths 审批路径（批准 / 拒绝 / 无人值守 fail-closed）、
  unsandbox 强制审批、非 macOS 行为回归
- 手工冒烟：`sandbox-exec -f <profile> cat ~/.ssh/id_rsa` 必须
  `Operation not permitted`——profile 若被改坏，这条会立刻暴露。

## 13. 威胁模型修正

`docs/shell_permission.md` 第 7 节当前写的是"防模型手滑的护栏，非安全沙箱；
OS 级隔离留作后续可选层"。本方案落地后应改为：

> shell 在 macOS 上受内核 Seatbelt 强制约束（S1 / S1n / S2 / SX 四档），
> 破坏半径被限定在项目根 + 会话目录 + 用户批准的扩权路径；人工审批的对象
> 从"命令"收敛为"路径扩权或脱离沙箱"。`read` / `write` / `edit` 受同一份
> 应用层路径策略约束。**仍未覆盖**：S1 网络放行下的数据外泄、
> 宿主进程内存中的凭证、跨重启的自改源码、会话历史自改。
> 需要对抗性隔离时使用虚拟机。
