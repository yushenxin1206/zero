# Zero

一个专业高效的 AI 助手，支持 CLI、飞书与 pywebview 桌面窗口三种通道，共享同一个消息总线与 Agent 循环。

## 环境要求

- Python >= 3.12
- [uv](https://docs.astral.sh/uv/) 管理环境与依赖
- 主平台 macOS（权限体系依赖 Seatbelt）；Windows 可运行 CLI / 飞书 / Web，沙箱相关用例自动跳过

## 安装

```bash
# 克隆/进入项目目录后，按 uv.lock 创建虚拟环境并安装依赖
uv sync

# 桌面窗口（pywebview）是可选依赖
uv sync --extra web

# 开发依赖（pytest / ruff）
uv sync --dev
```

`uv run <command>` 会在项目虚拟环境中执行命令，无需手动激活。

## 运行

```bash
# 默认同时启动 CLI + 飞书通道（未配置飞书 app_id 时自动跳过飞书）
uv run zero

# 只运行 CLI 通道
uv run zero --cli

# 以 pywebview 桌面窗口运行 web 通道
uv run zero --web
```

Windows 下等价的激活方式为 `.venv\Scripts\Activate.ps1`，入口为 `.venv\Scripts\zero.exe`。

### 免激活全局使用（Windows，可选）

希望在任意终端、不激活虚拟环境也能使用 `zero`，可以执行 `setup_zero_path.ps1`：

1. 先在用户目录创建 `bin\zero.cmd` shim，转发到 `<项目路径>\.venv\Scripts\zero.exe`（项目路径取脚本所在目录，自动推导）；
2. 再把该 `bin` 目录加入用户 PATH。

## 测试与静态检查

```bash
# 全量测试（一条命令，平台不适用的用例自动 skip）
uv run pytest

# 静态检查
uv run ruff check .
```

pytest 配置与 ruff 规则见 `pyproject.toml`。测试不依赖本机真实数据：`tests/conftest.py` 会把 HOME、会话、调度器与沙箱 profile 全部重定向到临时目录。

## 配置

配置文件位于 `~/.zero/config.json`，**需要自己创建**：程序不会自动生成，缺失时按默认值运行。

```bash
uv run python -m zero.config           # 把完整默认值模板打印到终端
uv run python -m zero.config --write   # 仅在配置文件不存在时写入模板
```

`--write` 遇到已有配置会拒绝覆盖，避免把手工维护的精简配置冲成一份全显式的。配置里**只写你真正要改的字段**；没写的字段继续继承（`agentRuntimes.compact` 等任务条目继承 `agentRuntimes.default`，其余用默认值）。需要飞书通道时填入：

```json
{
  "channels": {
    "feishu": {
      "appId": "你的 app_id",
      "appSecret": "你的 app_secret"
    }
  }
}
```

长期记忆存放在 `~/.zero/memory.md`，运行日志写入 `~/.zero/debug.log`（不污染终端界面）。
