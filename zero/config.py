import argparse
import json
import sys
from typing import Any

from pydantic import Field, PrivateAttr
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

    # 这份配置是从哪个文件读出来的。只有非空时才允许 save_config 回写：
    # Config 继承 BaseSettings，Config() 会把环境变量算作显式字段，若不设
    # 这个来源标记，环境变量就会被固化成磁盘配置。仅由 from_file 写入，
    # 外部通过只读属性 loaded_from 读取。
    _loaded_from: Path | None = PrivateAttr(default=None)

    @classmethod
    def from_file(cls, path: Path) -> 'Config':
        """从文件读取配置，并记住来源（save_config 只允许回写这个文件）。"""
        with open(path, 'r', encoding='utf-8') as f:
            config = cls.model_validate(json.load(f))
        config._loaded_from = path
        return config

    @property
    def loaded_from(self) -> Path | None:
        """来源文件；None 表示不是从文件读出的（默认值或环境变量）。"""
        return self._loaded_from

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


class ConfigWriteError(RuntimeError):
    """配置写盘被拒绝：来源不可信，或目标已存在。"""


def render_template() -> str:
    """完整默认值模板（只供展示 / 新建文件，绝不用于回写已有配置）。

    用 ``model_validate({})`` 而不是 ``Config()``：后者会读环境变量，
    模板里混进环境变量再存盘，就等于把环境变量固化成了配置。
    """
    return Config.model_validate({}).model_dump_json(indent=2, by_alias=True)


def write_template(path: Path | None = None) -> Path:
    """把模板写成一份新配置；目标已存在则拒绝，绝不覆盖手写内容。"""
    target = path or current().config
    if target.exists():
        raise ConfigWriteError(
            f'{target} 已存在，拒绝覆盖：模板只用于新建，改配置请手动编辑')
    save_or_create_file(file_path=target, content=render_template())
    return target


def save_config(config: Config) -> bool:
    """把配置的显式字段原子写回它来自的那个文件，返回是否真的写了。

    三条刻意设计的约束：

    1. 只写 ``model_fields_set``（exclude_unset，递归生效）。没写的键保持
       缺席，而「没写 = 继承 agentRuntimes.default / 使用默认值」正是配置
       的语义；全字段 dump 会把继承拍平成明文，从此不再跟随上层。
    2. 只接受 ``Config.from_file`` / ``load_config`` 读出来的配置。``Config``
       继承 BaseSettings，``Config()`` 会把环境变量算作显式字段；允许凭空
       回写的话，环境变量就会被焊进磁盘。来源文件也必须是当前数据根的那个
       config.json，跨文件回写一律拒绝。
    3. 语义内容与磁盘相同则不写：不碰 mtime，也不做无意义的 IO。

    写回目标固定为 ``current().config``，所以参数里没有 path——目标若可变，
    来源校验就只能在两个 Path 之间比较，语义反而含糊。
    """
    target = current().config
    origin = config.loaded_from
    if origin is None:
        raise ConfigWriteError(
            '配置不是从文件读出的（可能来自环境变量或默认值），拒绝写回：'
            '请只回写 load_config 返回的 Config')
    if origin != target:
        raise ConfigWriteError(
            f'配置来自 {origin}，不能写回 {target}')
    if not target.exists():
        raise ConfigWriteError(f'{target} 不存在，无法回写')

    with open(target, 'r', encoding='utf-8') as f:
        disk = json.load(f)
    data: dict[str, Any] = config.model_dump(
        mode='json', by_alias=True, exclude_unset=True)
    if data == disk:
        return False
    save_or_create_file(
        file_path=target,
        content=json.dumps(data, ensure_ascii=False, indent=2),
    )
    return True


def load_config(path: Path | None = None) -> Config:
    """显式读取配置。import 时绝不调用；调用方自己决定读取时机。

    ``path`` 缺省取当前数据根的 ``config.json``，不存在时返回全默认 Config
    （不带来源标记，因此不会被 save_config 回写）。
    """
    target = path or current().config
    if target.exists():
        return Config.from_file(target)
    return Config()


def main(argv: list[str] | None = None) -> int:
    """``python -m zero.config``：默认只打印模板，``--write`` 才落盘。"""
    parser = argparse.ArgumentParser(
        prog='python -m zero.config',
        description='打印配置模板；默认不写任何文件。')
    parser.add_argument(
        '--write', action='store_true',
        help='把模板写入配置文件（目标已存在时拒绝）')
    args = parser.parse_args(argv)

    if not args.write:
        print(render_template())
        return 0
    try:
        target = write_template()
    except ConfigWriteError as exc:
        print(f'未写入：{exc}', file=sys.stderr)
        return 1
    print(f'已写入 {target}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())


