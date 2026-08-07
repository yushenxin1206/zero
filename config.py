import json

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path

from utils.file import save_or_create_file

HOME = Path.home()
ZERO_ROOT = HOME / '.zero'
CONFIG_PATH = ZERO_ROOT / 'config.json'


class Base(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class Provider(Base):
    api_base: str = ''
    api_key: str = ''


class Providers(Base):
    dashscope: Provider = Field(default_factory=Provider)


class Defaults(Base):
    workspace: Path = ZERO_ROOT / 'workspace'
    provider: str = 'dashscope'
    model: str = 'qwen3.8-max'
    max_tokens: int = 8192
    context_window_tokens: int = 65536
    temperature: float | int = 0.1


class Config(BaseSettings):
    defaults: Defaults = Field(default_factory=Defaults)
    providers: Providers = Field(default_factory=Providers)

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


if __name__ == '__main__':
    print(load_config())

