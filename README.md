# Zero

一个专业高效的命令行 AI 助手，支持 CLI 与飞书双通道，共享同一个消息总线与 Agent 循环。

## 环境要求

- Python >= 3.14.5
- 推荐使用 [uv](https://docs.astral.sh/uv/) 管理环境与依赖

## 安装

```powershell
# 克隆/进入项目目录后，创建虚拟环境（已存在则跳过）
uv venv

# 以可编辑模式安装，生成 zero 命令入口
uv pip install -e .
```

安装成功后，虚拟环境的 `Scripts` 目录下会生成 `zero.exe`，其内嵌解释器路径指向该虚拟环境，不依赖全局 Python。

## 运行

```powershell
# 激活虚拟环境
.venv\Scripts\Activate.ps1

# 默认同时启动 CLI + 飞书通道（未配置飞书 app_id 时自动跳过飞书）
zero

# 只运行 CLI 通道
zero --cli
```

激活虚拟环境后，`zero` 可在任意目录下运行；system prompt 中的工作目录会实时跟随命令实际运行时所在的目录。

### 免激活全局使用（可选）

希望在任意终端、不激活虚拟环境也能使用 `zero`，可以执行 `setup_zero_path.ps1`：

1. 先在用户目录创建 `bin\zero.cmd` shim，转发到 `<项目路径>\.venv\Scripts\zero.exe`（项目路径取脚本所在目录，自动推导）；
2. 再把该 `bin` 目录加入用户 PATH。

## 配置

配置文件位于 `~/.zero/config.json`，首次运行自动生成默认配置。需要飞书通道时填入：

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
