# Zero

本地单用户的 AI Agent。桌面 Web、CLI、飞书三个通道共享同一条消息总线与 Agent 循环；会话、记忆、审批、技能、定时任务全部落在本地 `~/.zero`。

## 特性

| 能力 | 说明 |
|---|---|
| 三通道 | pywebview 桌面窗口、终端 CLI、飞书长连接；按渠道隔离会话，同一套核心逻辑 |
| 多模型 | 任意 OpenAI-compatible 端点；模型注册表声明 provider 与上下文窗口；支持按会话切模型 |
| 工具调用 | 文件读写、精确替换、shell、定时任务、后台子 agent |
| 权限边界 | P/G/D 三档 + macOS Seatbelt 内核沙箱；审批就地弹出，审计独立落盘 |
| 会话 | append-only JSONL、轮次边界、索引、重命名/删除、会话级工作目录 |
| 上下文 | 自动压缩、会话复盘、长期记忆与项目记忆 |
| 技能 | 内置技能随包只读；用户技能写在 `~/.zero/skills`，运行期可增改 |
| 自动化 | 一次性 / cron 定时任务；并行子 agent 后台执行并回投结果 |
| 桌面体验 | 日间/夜间主题、流式输出、可折叠工具与审批、本轮改动清单、路径与网址可点 |

## 环境要求

- Python >= 3.12
- [uv](https://docs.astral.sh/uv/) 管理环境与依赖
- 主平台 macOS（权限体系依赖 Seatbelt）；Windows 可运行 CLI / 飞书 / Web，但没有内核沙箱，shell 命令一律走审批
- 打包 macOS 应用需要 Xcode Command Line Tools（`clang`、`lipo`、`sips`、`iconutil`、`ditto`）

## 安装

```bash
# 按 uv.lock 创建虚拟环境并安装依赖
uv sync

# 桌面窗口（pywebview）是可选依赖
uv sync --extra web

# 开发依赖（pytest / ruff）
uv sync --dev
```

`uv run <command>` 会在项目虚拟环境中执行命令，无需手动激活。

## 快速开始

```bash
# 不带参数：打开桌面窗口
uv run zero

# 只启动 CLI
uv run zero --cli

# 只启动飞书通道（需在配置里填写 appId / appSecret）
uv run zero --feishu

# 多通道组合：桌面窗口 + CLI 同时启动
uv run zero --web --cli
```

`--web` / `--cli` / `--feishu` 可任意组合，不带任何开关时等价于 `--web`。

首次启动时会进入**配置引导页**：填入 provider 端点、API Key、模型名与上下文窗口后才能开始对话。CLI 与飞书没有引导页，配置未就绪时会直接提示先完成配置。

Windows 下等价的激活方式为 `.venv\Scripts\Activate.ps1`，入口为 `.venv\Scripts\zero.exe`。

### 免激活全局使用（Windows，可选）

希望在任意终端、不激活虚拟环境也能使用 `zero`，可以执行 `setup_zero_path.ps1`：

1. 在用户目录创建 `bin\zero.cmd` shim，转发到 `<项目路径>\.venv\Scripts\zero.exe`（项目路径取脚本所在目录）；
2. 把该 `bin` 目录加入用户 PATH。

## 配置

配置文件位于 `~/.zero/config.json`，程序不会自动生成。两种创建方式：

```bash
# 打印全量参考模板（只读，不写盘）
uv run python -m zero.config

# 写入一份能直接跑起来的精简骨架（目标已存在时拒绝覆盖）
uv run python -m zero.config --write
```

也可以直接使用桌面窗口的引导页。配置键使用 camelCase，**只写你真正要改的字段**：派生任务（`compact` / `session_summary` / `memory` / `subagent`）不写就继承 `agentRuntimes.default`。

最小可用配置：

```json
{
  "models": {
    "deepseek-chat": {
      "provider": "deepseek",
      "contextWindowTokens": 65536
    }
  },
  "providers": {
    "deepseek": {
      "apiBase": "https://api.deepseek.com/chat/completions",
      "apiKey": "你的 API Key"
    }
  },
  "agentRuntimes": {
    "default": {
      "llmCallOptions": {
        "model": "deepseek-chat"
      }
    }
  }
}
```

几个硬规则：

- `providers.<key>.apiBase` 会被原样当作 POST URL，必须写到 `/chat/completions` 结尾；只写到基址会得到 404。
- `models.<name>.provider` 必须指向已存在的 provider；`contextWindowTokens` 必填，它决定上下文压缩的触发点。
- `agentRuntimes.default.llmCallOptions.model` 必须指向 `models` 注册表中的键；派生任务未单独绑定时跟随它。
- 配置校验在启动 / 保存阶段完成；引导页与保存结果会直接指出要改哪个配置键。
- 程序创建的配置文件权限为 `0600`，仅属主可读写。

| 配置区块 | 作用 |
|---|---|
| `agentRuntimes` | 主对话与派生任务的模型、采样参数、最大迭代次数等 |
| `providers` | OpenAI-compatible 端点与密钥，键名可自定义 |
| `models` | 模型 → provider + 上下文窗口的接入事实 |
| `channels.web` | 本地 WebSocket 的 host / port 与窗口尺寸 |
| `channels.feishu` | 飞书通道开关与 `appId` / `appSecret` |
| `compact` | 上下文压缩开关、触发比例、尾部比例与输出上限 |
| `maintenance` | 会话复盘与长期记忆沉淀的开关、cron 与输出上限 |

### 界面内修改配置

桌面窗口的「设置 → 配置文件」提供表单与 JSON 两个标签页，覆盖全部配置字段，并提供「测试连接」和「备份并重置」。保存只落盘、不热更新，界面会提示**重启后生效**；顶部横幅可直接重启应用。

### 飞书凭据

在「设置 → 飞书」里可以直接扫码创建自建应用，也可以手动填写 `appId` / `appSecret`。扫码创建的凭据会先做令牌预检再落盘，成功后需要重启应用才会使用新凭据。

## 使用

### 桌面 Web

- 会话：新建、切换、重命名、删除；删除会移除该会话的消息、摘要与审批记录。
- 输入区：选择会话工作目录、切换模型、发送与停止（`Esc`）。
- 消息：流式输出、Markdown 渲染；工具调用、审批与思考内容实时展开，轮次结束后统一折叠；每条用户 / 助手消息带时间戳，助手回复在同一行页脚展示 token 用量。
- 本轮改动：助手回答末尾列出本轮新增/修改/删除的文件，按目录折叠，依赖与缓存目录汇总计数，目录行可点击定位。
- 链接：正文与行内代码中的绝对路径、`http(s)` 网址可点击；路径在文件管理器中打开或定位，网址用系统默认浏览器打开，不执行任何脚本。
- 设置入口：配置文件、权限、飞书、技能；顶栏另有定时任务面板，可查看当前会话或全部会话的任务。
- 主题：日间 / 夜间切换，选择持久化到 `~/.zero/web-theme`，重启后保持。
- 本地服务默认只监听 `127.0.0.1`，WebSocket 连接需要一次性 token。

### CLI

启动后输入 `/` 自动弹出命令补全（按使用频率排序），`Tab` 选择，`→` 或 `Ctrl+E` 接受灰色建议。

| 命令 | 作用 |
|---|---|
| `/new` | 新建会话 |
| `/sessions` | 选择 / 切换历史会话 |
| `/title [文本]` | 查看 / 设置当前会话标题 |
| `/model` | 查看 / 切换本会话模型 |
| `/cwd [绝对路径\|clear]` | 查看 / 设置 / 清除会话工作目录 |
| `/tasks [all]` | 查看当前会话（或全部）定时任务 |
| `/bgstream [on\|off]` | 开关定时任务结果的流式输出 |
| `/history` | 分页查看本会话全部历史 |
| `/help` | 显示可用命令 |
| `/quit` | 退出 CLI |

审批在终端就地完成；`/bgstream on` 时定时任务的输出会实时打印在输入行上方。

### 飞书

通过 WebSocket 长连接接收单聊消息与群里 @机器人 的消息，回复文本或富文本。连接状态、启停与重新连接都在「设置 → 飞书」里操作。

飞书属于无人值守渠道：不能弹出审批面板，需要审批的操作会直接失败；同时网络默认关闭。扫码创建应用只对企业租户稳定可用，个人版账号会被平台以 `21001` 拒绝。

## 工具

| 工具 | 行为 |
|---|---|
| `read` | 读取文件，支持 `start_line` / `end_line` 行范围 |
| `write` | 创建、覆盖或追加文件，自动创建父目录 |
| `edit` | 精确字符串替换；默认要求唯一匹配，可 `replace_all`；返回 diff |
| `shell` | 执行命令，默认超时 60 秒、最大 600 秒；可指定 `working_dir` 与越界写路径 `write_paths` |
| `create_scheduled_task` | 创建一次性（延迟 / ISO 时间）或 5 段 cron 定时任务 |
| `cancel_scheduled_task` | 取消当前会话中尚未终结的定时任务 |
| `subagent` | 把一批独立任务交给后台子 agent 并行执行，结果稍后作为新消息回投 |

工具结果超过 10000 字符会被截断，读大文件应使用行范围。

## 权限与沙箱

所有工具调用在真正执行前经过同一个权限守卫。判定看**资源**，不看命令文本：

| 档位 | 含义 | 处理 |
|---|---|---|
| P | 包络内 | 免审批 |
| G | 可授予 | 弹出审批；无人值守时拒绝 |
| D | 不可授予 | 直接拒绝，不提供审批 |

- **可写集合**：会话工作目录（用户选择后）、会话数据目录、TMPDIR、用户技能根、长期扩权根、本会话获批的临时根。打包后的应用包始终不可写。
- **读禁止**：`~/.ssh`、`~/.aws`、`~/.gnupg`、`~/.config/gh`、`~/.netrc`、`~/Library/Keychains`，以及 zero 自身的 `config.json`、`profile.json` 与调度数据；这些没有审批通道。
- **网络**：CLI / Web 中由用户触发的轮次允许联网；定时任务、子 agent、飞书等无人值守来源默认断网。
- **shell 执行形态**：POSIX 上是不带 shell 的直接 exec，`|`、`&&`、`>`、glob、`cd` 都不生效，一条命令只能是一个程序；Windows 走 `cmd.exe`。
- 越界写入会失败并提示；模型可带 `write_paths` 声明需求，由用户选择「本会话允许 / 总是允许 / 拒绝」。
- 审批记录写入会话目录的 `approvals.jsonl`，只用于历史回显，不进入模型上下文。
- 命中「脱离沙箱命令」白名单的命令强制审批，且只能按命令名配置。

命令行工具：

```bash
zero-permission check                  # 内核沙箱与断网档是否真的生效
zero-permission list                   # 当前写集合、网络策略、长期扩权
zero-permission add-root <path>        # 追加长期可写根
zero-permission add-unsandbox <name>   # 追加脱离沙箱命令（强制审批）
```

## 会话、上下文与记忆

```
~/.zero/sessions/{channel}/{date}/{session_id}/
├── messages.jsonl    # append-only 原始消息
├── compact.json      # 上下文压缩记录（可能不存在）
├── summary.json      # 会话复盘（可能不存在）
└── approvals.jsonl   # 审批审计
```

- 渠道级 `index.json` 只存元数据（标题/描述），活跃时间取 `messages.jsonl` 的 mtime，每轮对话不重写索引。
- 每轮用 `turn_begin` / `turn_end` 标记边界；加载时丢弃未闭合轮次，并在已闭合轮次中补齐缺失的工具结果占位。
- 主对话历史超过窗口阈值时自动压缩：压缩摘要作为前缀，尾部保留最近消息。
- 会话复盘按 cron 在后台运行，把结构化总结写入 `summary.json`，并把标题/描述回写到索引；长期记忆任务再把复盘合并进 `~/.zero/memory.md`。
- 用户记忆由模型按需维护；选定会话工作目录后，还会注入项目记忆 `<工作目录>/.zero/memory/memory.md`。
- 会话级模型绑定会随会话保存；模型从配置中删除后自动回退默认模型。

默认维护时间：会话复盘每天 23:00，记忆沉淀每天 23:30；可通过 `maintenance.sessionSummaryCron` / `maintenance.memoryCron` 修改。错过的槽位会在启动后补跑。

## 技能

技能是给模型按需加载的操作说明：命中 `description` 描述的触发条件时才读取正文，不常驻上下文。

| 内置技能 | 作用 |
|---|---|
| `plan` | 复杂任务的规划-评审-执行-跟踪；计划落盘为会话目录的 `plan.json` / `plan.md` |
| `session-history` | 三层检索历史会话：索引 → 会话摘要 → 会话详情 |
| `web-search` | 通过 Bing 网页搜索获取实时信息，无需 API Key |
| `skill-authoring` | 指导 agent 创建或修改用户技能 |

用户技能放在 `~/.zero/skills/<name>/skill.md`，运行期可写，agent 新增或修改时无需逐文件审批；同名技能会覆盖内置版本。支持文件放在同一技能目录下（`references/`、`scripts/` 等），正文用相对路径引用。新增或修改后下一轮对话自动生效，无需重启。

## 定时任务与子 agent

- 定时任务支持一次性（延迟分钟或 ISO 时间）与 5 段 cron（`*`、列表、范围、步长）。任务在所属会话上下文中执行，结果回投原会话。
- 定时任务属于无人值守来源：没有审批面板，网络默认关闭；一次性任务在轮次真正结束后才回写 `completed` / `failed` / `cancelled`，派发时不会提前标完成。
- 子 agent 通过 `subagent` 工具并行启动，每个任务在隔离上下文中单次执行，工具集与主对话同源（仅禁止递归派生 `subagent`），不落自己的会话历史；完成后结果作为新轮次回到主对话。需要审批的操作在后台无人值守，直接失败而不会挂起等审批。
- 子 agent 可单独绑定模型：在 `agentRuntimes.subagent` 里指定更便宜的模型即可。

## 数据目录

| 路径 | 内容 |
|---|---|
| `~/.zero/config.json` | 配置（权限 `0600`） |
| `~/.zero/profile.json` | 权限长期扩权根与脱离沙箱白名单（模型不可写） |
| `~/.zero/memory.md` | 用户长期记忆 |
| `~/.zero/skills/` | 用户技能 |
| `~/.zero/sessions/` | 会话历史、索引、摘要、压缩与审批记录 |
| `~/.zero/scheduler/tasks.jsonl` | 定时任务 |
| `~/.zero/maintenance/state.json` | 维护任务最近运行的槽位 |
| `~/.zero/logs/debug.log` | 运行日志（2MB × 3 滚动，密钥脱敏） |
| `~/.zero/web-theme` | 桌面窗口主题偏好 |
| `~/.zero/run/` | 沙箱 profile 临时文件 |

## 项目结构

| 路径 | 职责 |
|---|---|
| `zero/main.py` | 组合根：读取配置、构造核心组件、启动通道 |
| `zero/bus/` | 进程内消息总线（入站、出站、流式、审批） |
| `zero/core/` | Agent 循环、runner、上下文、压缩、hooks、取消、本轮改动 |
| `zero/channels/` | 共享 `ChannelService` 与 CLI / Web / 飞书三通道 |
| `zero/tools/` | read / write / edit / shell / 定时任务 / 子 agent |
| `zero/permission/` | 能力包络、权限守卫、macOS Seatbelt 后端 |
| `zero/providers/` | OpenAI-compatible 协议实现与重试 |
| `zero/session/` | 会话存储、索引、摘要模型 |
| `zero/scheduler/` | cron 解析、任务存储、调度引擎 |
| `zero/subagent/` | 子 agent 并行调度与结果回投 |
| `zero/maintenance/` | 会话复盘与长期记忆沉淀 |
| `zero/skills/` | 内置技能（随包只读） |
| `zero/channels/web/static/` | 单文件桌面界面 |

## 开发与测试

```bash
# 全量测试（平台不适用的用例自动 skip）
uv run pytest

# 静态检查
uv run ruff check .
```

pytest 配置与 ruff 规则见 `pyproject.toml`。测试不依赖本机真实数据：`tests/conftest.py` 会把 HOME、会话、调度器与沙箱 profile 全部重定向到临时目录。

CI 在 `macos-latest` 上以 Python 3.12 / 3.13 运行 `ruff check .` 与全量 `pytest`，见 [ci.yml](.github/workflows/ci.yml)。

## 打包 macOS 应用

```bash
# 本机架构
.venv/bin/python scripts/build_macos_app.py

# 显式指定架构（本机没有对应解释器时会直接失败）
.venv/bin/python scripts/build_macos_app.py --arch arm64
```

产物在 `dist/<arch>/Zero.app`，每次构建会先清空目标架构的中间目录与产物。当前产物未签名，仅适合本机 / 内部验证；架构规则、签名公证与冒烟清单见 [docs/packaging.md](docs/packaging.md)。

## 已知限制

- 未签名、未公证，分发给他人前必须走签名流程。
- 内核沙箱只在 macOS 生效；其他平台没有「包络内免审批」，shell 命令一律审批。
- 飞书个人版账号无法通过扫码创建可用应用。
- 把 `channels.web.host` 改成 `0.0.0.0` 会把可执行 shell 的服务暴露到局域网，除非明确需要，否则保持回环地址。
- 多模态输入（图片）尚未实现，设计方案见 [docs/multimodal_input.md](docs/multimodal_input.md)（当前搁置）。
- POSIX 下 shell 不支持管道、重定向与 `&&`，这是权限与沙箱模型的明确取舍。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/packaging.md](docs/packaging.md) | macOS 打包、架构规则、签名与冒烟清单 |
| [docs/sandbox.md](docs/sandbox.md) | 权限包络、Seatbelt 沙箱与已知残留 |
| [docs/shell_capability_approval.md](docs/shell_capability_approval.md) | 能力授权式审批的决策记录 |
| [docs/hooks.md](docs/hooks.md) | `before_tool_call` hook 契约 |
| [docs/skill.md](docs/skill.md) | skill.md 模板与写作约定 |
| [docs/prompt.md](docs/prompt.md) | 系统提示词样例与说明 |
| [docs/architecture_refactor.md](docs/architecture_refactor.md) | 架构改造结果与取舍 |
| [docs/architecture-review.md](docs/architecture-review.md) | 独立架构评审快照 |
