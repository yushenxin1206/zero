import json

from pydantic import Field
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path

from zero.models import AgentRuntimeConfig, ChannelConfigs, MaintenanceConfig, ProviderConfigs
from zero.utils.file import save_or_create_file

HOME = Path.home()
ZERO_ROOT = HOME / '.zero'
CONFIG_PATH = ZERO_ROOT / 'config.json'
MEMORY_PATH = ZERO_ROOT / "memory.md"

# 工具结果截断阈值（字符）。core/runner.py 执行截断、core/context.py
# 提示词说明共用同一值，只在此定义一次，防止两处漂移。
TOOL_RESULT_MAX_CHARS = 10000


class Config(BaseSettings):
    agent_runtime: AgentRuntimeConfig = Field(default_factory=AgentRuntimeConfig)
    providers: ProviderConfigs = Field(default_factory=ProviderConfigs)
    channels: ChannelConfigs = Field(default_factory=ChannelConfigs)
    maintenance: MaintenanceConfig = Field(default_factory=MaintenanceConfig)

    model_config = SettingsConfigDict(alias_generator=to_camel, populate_by_name=True)


def save_config(config: Config):
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


