import json

from pydantic import Field
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path

from datacls import AgentRuntimeConfig, ChannelConfigs, ProviderConfigs
from utils.file import save_or_create_file

HOME = Path.home()
ZERO_ROOT = HOME / '.zero'
CONFIG_PATH = ZERO_ROOT / 'config.json'


class Config(BaseSettings):
    agent_runtime: AgentRuntimeConfig = Field(default_factory=AgentRuntimeConfig)
    providers: ProviderConfigs = Field(default_factory=ProviderConfigs)
    channels: ChannelConfigs = Field(default_factory=ChannelConfigs)

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
    pass
    # save_config(Config())

