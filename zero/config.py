import json
from typing import Any

from pydantic import Field
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path

from zero.models import (
    AgentRuntimeConfig,
    AgentRuntimeSpecs,
    ChannelConfigs,
    CompactConfig,
    LLMRuntime,
    MaintenanceConfig,
    ModelSpec,
    ProviderConfig,
    ProviderConfigs,
)
from zero.paths import current
from zero.utils.file import save_or_create_file

HOME = Path.home()

# 项目级记忆的存放目录名（相对运行目录）。项目级记忆由模型用 write/edit
# 工具维护，代码侧只读不写。
PROJECT_ZERO_DIRNAME = ".zero"


def project_memory_path(cwd: Path) -> Path:
    """项目级记忆文件路径：<cwd>/.zero/memory/memory.md。

    与用户级记忆（``current().memory``）区分：后者是跨项目的用户偏好
    与背景，前者是当前项目的持久事实与约定。cwd 由调用方传入（通常
    core/context.get_cwd()），避免本模块反向依赖 core。显式配置了
    ``Paths.project_memory`` 时以它为准（测试/嵌入式宿主用）。
    """
    override = current().project_memory
    if override is not None:
        return override
    return cwd / PROJECT_ZERO_DIRNAME / "memory" / "memory.md"

# 工具结果截断阈值（字符）。core/runner.py 执行截断、core/context.py
# 提示词说明共用同一值，只在此定义一次，防止两处漂移。
TOOL_RESULT_MAX_CHARS = 10000


class Config(BaseSettings):
    # 模型注册表：键 = API 模型名，值 = 接入事实（provider / 上下文窗口）
    models: dict[str, ModelSpec] = Field(default_factory=dict)
    # 每个任务一套 agent runtime：default 主对话 / compact / session_summary / memory
    agent_runtimes: AgentRuntimeSpecs = Field(default_factory=AgentRuntimeSpecs)
    providers: ProviderConfigs = Field(default_factory=ProviderConfigs)
    channels: ChannelConfigs = Field(default_factory=ChannelConfigs)
    compact: CompactConfig = Field(default_factory=CompactConfig)
    maintenance: MaintenanceConfig = Field(default_factory=MaintenanceConfig)

    model_config = SettingsConfigDict(alias_generator=to_camel, populate_by_name=True)

    def resolve_runtime(self, task: str, model_override: str | None = None) -> AgentRuntimeConfig:
        """按任务名解析出物化的运行时配置（配置层唯一的解析入口）。

        字段优先级统一为：agentRuntimes[task] 显式值 > agentRuntimes.default
        显式值 > 数据模型默认值。策略字段与 llm_call_options 共用这一条链
        （一层 merge，见 _merge_runtime），model 只是 llm_call_options 里的
        一个普通字段——它同时决定 llm_runtime（provider / 上下文窗口），
        为空则立即报错（不静默回退到某个默认模型）。provider 由
        ModelSpec.provider 键在 providers 中结算成对象，结算不到立即报错。

        model_override：非空时用注册表键覆盖合并出的 model（会话级模型绑定
        的注入点）。窗口 / provider 按该模型查表结算，llm_call_options 同步
        写回新模型名（请求体用新模型）。缺省 None 时行为与旧版完全一致。
        """
        explicit = self._merge_runtime(task)
        llm_explicit = explicit.get('llm_call_options', {}) or {}
        model_name = llm_explicit.get('model', '')
        if model_override:
            model_name = model_override
            explicit.setdefault('llm_call_options', {})['model'] = model_override
        if not model_name:
            raise ValueError(
                f"任务 {task!r} 未绑定模型（agentRuntimes.default 也未绑定）：请在 "
                f"config.json 的 agentRuntimes.default.llmCallOptions.model 指定 "
                f"models 注册表中的键")
        ms = self.models.get(model_name)
        if ms is None:
            raise ValueError(
                f"模型 {model_name!r} 不在 models 注册表中，"
                f"已知键: {sorted(self.models)}")

        return AgentRuntimeConfig(
            **explicit,
            llm_runtime=LLMRuntime(
                provider=self._resolve_provider(ms.provider),
                context_window_tokens=ms.context_window_tokens,
            ),
        )

    def _merge_runtime(self, task: str) -> dict[str, Any]:
        """合并 agentRuntimes.default 与 agentRuntimes[task] 的显式字段。

        优先级 task > default：只取各层"显式写过"的键（exclude_unset，即
        磁盘文件里的键集合），空串 / null 视为未设置。策略字段是标量，逐层
        覆盖即可；llmCallOptions 是嵌套模型，必须逐子键合并——整条 replace
        会把 default 里的 model 丢掉，导致派生任务意外失绑。返回值不含任何
        默认值，缺省字段由 AgentRuntimeConfig / LLMCallOptions 构造时统一
        落回数据模型默认值。
        """
        merged: dict[str, Any] = {}
        for cfg in (self.agent_runtimes.get('default'),
                    self.agent_runtimes.get(task)):
            if cfg is None:
                continue
            for k, v in cfg.model_dump(exclude_unset=True).items():
                if v is None or v == '':
                    continue
                if k == 'llm_call_options':
                    merged.setdefault(k, {}).update(
                        {lk: lv for lk, lv in v.items()
                         if lv is not None and lv != ''})
                else:
                    merged[k] = v
        return merged

    def _resolve_provider(self, provider_key: str) -> ProviderConfig:
        """按 ModelSpec.provider 键在 providers 中结算出 ProviderConfig。

        结算不到（键不在 ProviderConfigs 的字段里，即未在 providers 配置该
        厂商）立即报错，防止构造出带空 api_base/api_key 的 client 后才在
        请求时失败。
        """
        known = type(self.providers).model_fields
        if provider_key not in known:
            raise ValueError(
                f"provider {provider_key!r} 未在 providers 中配置，"
                f"已知键: {sorted(known)}")
        return getattr(self.providers, provider_key)


def save_config(config: Config, path: Path | None = None):
    """写出配置模板。``path`` 缺省取当前数据根，测试/嵌入可显式指定。"""
    # 仅人工手动运行（python -m zero.config）生成 config.json 模板，运行时
    # 不会调用，故用全字段 dump 展示完整默认值结构。注意：勿在已含
    # agentRuntimes 条目的配置上重跑——全字段展开会把磁盘上精简的条目污染
    # 成全显式（fields_set 来自磁盘键集合），各任务条目因此失去对 default
    # 的字段级继承（例如 compact 会带上显式的默认 temperature）。
    config_json = config.model_dump_json(indent=2, by_alias=True)
    save_or_create_file(
        file_path=path or current().config,
        content=config_json
    )


def load_config(path: Path | None = None) -> Config:
    """显式读取配置。import 时绝不调用；调用方自己决定读取时机。

    ``path`` 缺省取当前数据根的 ``config.json``，不存在时返回全默认 Config。
    """
    target = path or current().config
    if target.exists():
        with open(target, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return Config.model_validate(data)
    return Config()


if __name__ == '__main__':
    save_config(load_config())


