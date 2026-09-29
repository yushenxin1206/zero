import json

from pydantic import Field
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path

from zero.models import (
    AgentRuntimeSpec,
    ChannelConfigs,
    MaintenanceConfig,
    ModelSpec,
    ProviderConfigs,
    TaskConfig,
    TaskConfigs,
)
from zero.utils.file import save_or_create_file

HOME = Path.home()
ZERO_ROOT = HOME / '.zero'
CONFIG_PATH = ZERO_ROOT / 'config.json'
MEMORY_PATH = ZERO_ROOT / "memory.md"

# 工具结果截断阈值（字符）。core/runner.py 执行截断、core/context.py
# 提示词说明共用同一值，只在此定义一次，防止两处漂移。
TOOL_RESULT_MAX_CHARS = 10000


class Config(BaseSettings):
    agent_runtime: AgentRuntimeSpec = Field(default_factory=AgentRuntimeSpec)
    # 模型注册表：键 = API 模型名，值 = 接入事实（provider / 上下文窗口）
    models: dict[str, ModelSpec] = Field(default_factory=dict)
    # 任务级模型绑定：default 主对话 / compact / session_summary / memory
    tasks: TaskConfigs = Field(default_factory=TaskConfigs)
    providers: ProviderConfigs = Field(default_factory=ProviderConfigs)
    channels: ChannelConfigs = Field(default_factory=ChannelConfigs)
    maintenance: MaintenanceConfig = Field(default_factory=MaintenanceConfig)

    model_config = SettingsConfigDict(alias_generator=to_camel, populate_by_name=True)


def save_config(config: Config):
    # 仅人工手动运行（python -m zero.config）生成 config.json 模板，运行时
    # 不会调用，故用全字段 dump 展示完整默认值结构。注意：勿在已含 tasks
    # 条目的配置上重跑——全字段展开会把磁盘上精简的 llmConfig 污染成全
    # 显式（fields_set 来自磁盘键集合），compact 未写 maxTokens 时会被
    # 展开出的默认 8192 顶掉 2000 兜底。
    config_json = config.model_dump_json(indent=2, by_alias=True)
    save_or_create_file(
        file_path=CONFIG_PATH,
        content=config_json
    )


def load_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return Config.model_validate(data)
    else:
        return Config()


default_config = load_config()

if __name__ == '__main__':
    save_config(load_config())


