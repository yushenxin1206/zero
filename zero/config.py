import argparse
import copy
import json
import os
import re
import sys
from datetime import datetime
from typing import Any

from pydantic import Field, ValidationError, field_validator
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
from zero.data_paths import DataPaths
from zero.utils.file import save_or_create_file

HOME = Path.home()

# 项目级记忆的存放目录名（相对运行目录）。项目级记忆由模型用 write/edit
# 工具维护，代码侧只读不写。
PROJECT_ZERO_DIRNAME = ".zero"


def project_memory_path(cwd: Path, *, override: Path | None = None) -> Path:
    """项目级记忆文件路径：<cwd>/.zero/memory/memory.md。

    与用户级记忆（``DataPaths.memory``）区分：后者是跨项目的用户偏好
    与背景，前者是当前项目的持久事实与约定。cwd 由调用方传入（通常是
    ``Context.working_dir``），避免本模块反向依赖 core。显式配置了
    ``DataPaths.project_memory`` 时由调用方作为 override 传入（测试/嵌入式宿主用）。
    """
    if override is not None:
        return override
    return cwd / PROJECT_ZERO_DIRNAME / "memory" / "memory.md"

# 工具结果截断阈值（字符）。core/runner.py 执行截断、core/context.py
# 提示词说明共用同一值，只在此定义一次，防止两处漂移。
TOOL_RESULT_MAX_CHARS = 10000


# 内置厂商：无配置文件时仍默认提供，首次引导可直接选择；
# 用户也可以添加任意 OpenAI 兼容厂商。
PROVIDER_PRESETS: tuple[str, ...] = ('dashscope', 'deepseek', 'zhipu')
PROVIDER_KEY_RE = re.compile(r'^[A-Za-z0-9_-]+$')


def _default_providers() -> ProviderConfigs:
    return {key: ProviderConfig() for key in PROVIDER_PRESETS}


class Config(BaseSettings):
    # 每个任务一套 agent runtime：default 主对话 / compact / session_summary / memory
    agent_runtimes: AgentRuntimeSpecs = Field(default_factory=AgentRuntimeSpecs)
    providers: ProviderConfigs = Field(default_factory=_default_providers)
    # 模型注册表：键 = API 模型名，值 = 接入事实（provider / 上下文窗口）
    models: dict[str, ModelSpec] = Field(default_factory=dict)
    channels: ChannelConfigs = Field(default_factory=ChannelConfigs)
    compact: CompactConfig = Field(default_factory=CompactConfig)
    maintenance: MaintenanceConfig = Field(default_factory=MaintenanceConfig)

    model_config = SettingsConfigDict(alias_generator=to_camel, populate_by_name=True)

    @field_validator('providers')
    @classmethod
    def _check_provider_keys(cls, value: ProviderConfigs) -> ProviderConfigs:
        """provider 标识会出现在 models.provider 里，限制字符集避免手写歧义。"""
        for key in value:
            if not PROVIDER_KEY_RE.fullmatch(key):
                raise ValueError(
                    f'provider 标识 {key!r} 不合法：只能包含字母、数字、'
                    f'下划线和中划线')
        return value

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
        if ms.context_window_tokens <= 0:
            raise ValueError(
                f"模型 {model_name!r} 未声明上下文窗口：请在 config.json 的 "
                f"models.{model_name}.contextWindowTokens 填写该模型的上下文"
                f"长度（单位 token）。窗口决定压缩触发点，缺失会一直不压缩，"
                f"直到请求超窗报错")

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

        结算不到（键不在 providers 映射表里）立即报错，防止构造出带空
        api_base/api_key 的 client 后才在请求时失败。
        """
        provider = self.providers.get(provider_key)
        if provider is None:
            raise ValueError(
                f"provider {provider_key!r} 未在 providers 中配置，"
                f"已知键: {sorted(self.providers)}")
        return provider


class ConfigWriteError(RuntimeError):
    """配置写盘被拒绝：模板只允许新建文件，绝不覆盖已有配置。"""


# ``--write`` 落盘的启动骨架：只写"能解析、能绑定模型"的必要覆盖。
# compact / session_summary / memory / subagent 等派生任务刻意不出现——
# 它们靠继承 agentRuntimes.default 即可工作，落盘只会制造第二份真相。
SKELETON: dict[str, Any] = {
    "models": {
        "deepseek-chat": {
            "provider": "deepseek",
            "contextWindowTokens": 65536,
        },
    },
    "providers": {
        "deepseek": {
            # apiBase 被原样当 POST URL 用，必须写到 /chat/completions（完整端点）。
            # 只写到基址会让每个请求打到版本前缀上，厂商回 404。
            "apiBase": "https://api.deepseek.com/chat/completions",
            "apiKey": "",
        },
    },
    "agentRuntimes": {
        "default": {
            "llmCallOptions": {"model": "deepseek-chat"},
        },
    },
}


def render_template() -> str:
    """完整默认值模板（只供展示 / 新建文件，绝不用于回写已有配置）。

    用 ``model_construct()`` 而不是 ``model_validate({})``：``Config`` 继承
    BaseSettings，它的校验路径会走 settings 的 init，环境变量（如 ``MODELS``）
    仍会被读进来——那等于把环境变量混进模板、再被 ``--write`` 固化到磁盘。
    ``model_construct`` 只填字段默认值，不读任何来源，也不做校验；模板只是
    展示默认结构，不需要校验。默认值本身的合法性由测试
    ``test_template_is_a_valid_config`` 兜底。
    """
    return Config.model_construct().model_dump_json(indent=2, by_alias=True)


def render_skeleton() -> str:
    """``--write`` 的精简骨架：只覆盖建立模型绑定所需的最少字段。

    与 ``render_template`` 的分工：模板回答"全部可配置项是什么"，骨架回答
    "最少写多少就能跑"。派生任务的调用选项全部继承 default，用户后续只需
    替换 models / providers / default.model 里的示例值并填入 apiKey。
    """
    return json.dumps(SKELETON, ensure_ascii=False, indent=2)


def write_skeleton(path: Path) -> Path:
    """把骨架写成一份新配置；目标已存在则拒绝，绝不覆盖手写内容。"""
    target = path
    if target.exists():
        raise ConfigWriteError(
            f'{target} 已存在，拒绝覆盖：骨架只用于新建，改配置请手动编辑')
    save_or_create_file(file_path=target, content=render_skeleton())
    return target


def load_config(path: Path) -> Config:
    """显式读取配置。import 时绝不调用；调用方自己决定读取时机。

    路径由调用方提供（组合根传 ``DataPaths.config``）；不存在时返回全默认
    Config。配置没有运行时写入口：只有人手写文件，或用模板命令新建。
    """
    target = path
    if target.exists():
        with open(target, 'r', encoding='utf-8') as f:
            return Config.model_validate(json.load(f))
    return Config()


# ---------------------------------------------------------------------------
# 运行时配置读写（Web 引导页 / 设置面板）
# ---------------------------------------------------------------------------

# 传给前端的密钥占位：保存时仍是这个值就回填磁盘上的原 key，
# 避免密钥进入 DOM / 被误改写成占位符。
MASKED_SECRET = '****'


class ConfigValidationError(ValueError):
    """配置草稿未通过校验；issues 是给 UI 的字段级错误列表。"""

    def __init__(self, issues: list[dict[str, str]]):
        self.issues = issues
        super().__init__(
            '；'.join(i['message'] for i in issues) or '配置不合法')


def _issue(path: str, message: str) -> dict[str, str]:
    return {'path': path, 'message': message}


def _validation_issues(exc: ValidationError) -> list[dict[str, str]]:
    """把 pydantic 校验错误转成 {path, message}，路径直接给 UI 定位。"""
    issues = []
    for err in exc.errors():
        path = '.'.join(str(part) for part in err.get('loc', ()) if part != '')
        issues.append(_issue(path, str(err.get('msg') or '配置项不合法')))
    return issues or [_issue('', '配置不合法')]


def _binding_problem(cfg: Config) -> dict[str, str] | None:
    """配置结构合法但"跑不起来"的原因（缺注册表 / 未绑模型 / 缺端点）。"""
    if not cfg.models:
        return _issue('models', 'models 注册表为空：至少注册一个模型')
    # 每条模型都必须在 providers 里有对应项，且端点非空——否则拼错 provider
    # 要等到真正用到该模型时才报错。
    for name, spec in cfg.models.items():
        provider = cfg.providers.get(spec.provider)
        if provider is None:
            return _issue(
                f'models.{name}.provider',
                f'provider {spec.provider!r} 未在 providers 中配置，'
                f'已知键: {sorted(cfg.providers)}')
        if not provider.api_base.strip():
            return _issue(
                f'providers.{spec.provider}.apiBase',
                f'模型 {name!r} 使用的 provider {spec.provider!r} 未配置 apiBase')
    try:
        cfg.resolve_runtime('default')
    except ValueError as exc:
        return _issue('agentRuntimes.default.llmCallOptions.model', str(exc))
    return None


class ConfigManager:
    """config.json 的唯一读写入口：状态判定 + 校验 + 原子落盘。

    P1 语义：保存只落盘，**不在进程内替换配置**，重启后生效。这样不存在
    "部分生效" 的中间态，也不需要为每个缓存了 config 的组件写失效逻辑。

    ``config`` 与 ``raw`` 的分工：
    - ``raw`` 是磁盘上的 JSON 文档，保存时写它——不能 ``model_dump`` 回写，
      否则默认值和环境变量会被固化进文件；
    - ``config`` 是本进程实际使用的解析结果，供组合根/渠道读取。
    """

    def __init__(self, path: Path):
        self.path = path
        self.config: Config = Config()
        self.raw: dict[str, Any] = json.loads(render_skeleton())
        # ok | missing | invalid | unbound
        self.status: str = 'missing'
        self.issues: list[dict[str, str]] = []

    @property
    def runtime_ready(self) -> bool:
        """本进程当前配置能否真正跑起来。

        保存只改磁盘文档，不替换 ``config``，所以保存成功后这里仍为 False，
        直到重启重新加载——这正是 P1「保存 → 重启生效」的语义来源。
        """
        return bool(self.config.models) and _binding_problem(self.config) is None

    @property
    def needs_setup(self) -> bool:
        return not self.runtime_ready

    def load(self) -> Config:
        """读取并判定状态；任何失败都不抛，交给引导页展示。"""
        self.issues = []
        if not self.path.exists():
            self.raw = json.loads(render_skeleton())
            self.config = Config()
            # 环境变量可以完整提供配置（无文件的高级用法）：能跑起来就不必
            # 强制走引导页；否则交给首次配置向导。
            usable = bool(self.config.models) and not _binding_problem(self.config)
            self.status = 'ok' if usable else 'missing'
            return self.config
        try:
            doc = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            self.status = 'invalid'
            self.raw = {}
            self.config = Config()
            self.issues = [_issue('', f'无法解析 config.json：{exc}')]
            return self.config
        if not isinstance(doc, dict):
            self.status = 'invalid'
            self.raw = {}
            self.config = Config()
            self.issues = [_issue('', 'config.json 顶层必须是 JSON 对象')]
            return self.config
        try:
            cfg = Config.model_validate(doc)
        except ValidationError as exc:
            self.status = 'invalid'
            self.raw = doc
            self.config = Config()
            self.issues = _validation_issues(exc)
            return self.config
        self.raw = doc
        self.config = cfg
        problem = _binding_problem(cfg)
        self.status = 'unbound' if problem else 'ok'
        if problem:
            self.issues = [problem]
        return cfg

    def setup_payload(self) -> dict[str, Any]:
        """给前端的首次配置状态；required 为 True 时引导页不可跳过。"""
        ready = self.runtime_ready
        reason = 'ok' if ready else (
            self.status if self.status != 'ok' else 'unbound')
        return {
            'required': not ready,
            'reason': reason,
            'configPath': str(self.path),
            'issues': [] if ready else self.issues,
        }

    def masked_doc(self) -> dict[str, Any]:
        """可安全下发的配置文档：apiKey / appSecret 只给占位符。"""
        doc = copy.deepcopy(self.raw)
        providers = doc.get('providers')
        if isinstance(providers, dict):
            for value in providers.values():
                if isinstance(value, dict) and value.get('apiKey'):
                    value['apiKey'] = MASKED_SECRET
        feishu = None
        if isinstance(doc.get('channels'), dict):
            candidate = doc['channels'].get('feishu')
            feishu = candidate if isinstance(candidate, dict) else None
        if feishu and feishu.get('appSecret'):
            feishu['appSecret'] = MASKED_SECRET
        return doc

    def restore_secrets(self, doc: dict[str, Any]) -> dict[str, Any]:
        """把前端回传的占位符换回磁盘上的真实密钥。"""
        doc = copy.deepcopy(doc)
        providers = doc.get('providers')
        if not isinstance(providers, dict):
            providers = {}
        current = self.raw.get('providers')
        current = current if isinstance(current, dict) else {}
        for key, value in providers.items():
            if not isinstance(value, dict) or value.get('apiKey') != MASKED_SECRET:
                continue
            old = current.get(key)
            value['apiKey'] = (
                str(old.get('apiKey') or '') if isinstance(old, dict) else '')
        feishu = None
        if isinstance(doc.get('channels'), dict):
            candidate = doc['channels'].get('feishu')
            feishu = candidate if isinstance(candidate, dict) else None
        if feishu and feishu.get('appSecret') == MASKED_SECRET:
            old_channels = self.raw.get('channels')
            old_feishu = (old_channels.get('feishu')
                          if isinstance(old_channels, dict) else None)
            feishu['appSecret'] = (
                str(old_feishu.get('appSecret') or '')
                if isinstance(old_feishu, dict) else '')
        return doc

    def validate_doc(
            self, doc: dict[str, Any]) -> tuple[Config | None, list[dict[str, str]]]:
        """结构校验 + 可运行性校验；返回 (配置, 错误列表)。"""
        try:
            cfg = Config.model_validate(doc)
        except ValidationError as exc:
            return None, _validation_issues(exc)
        problem = _binding_problem(cfg)
        if problem:
            return None, [problem]
        return cfg, []

    def set_feishu_enabled(self, enabled: bool) -> None:
        """只改飞书通道开关并落盘，供运行期开关使用。

        不要求 UI 提交整份配置：避免把界面上未打开的其他字段（尤其是密钥
        占位）带进回写路径。校验与原子写入仍复用 ``save``。
        """
        doc = copy.deepcopy(self.raw)
        channels = doc.get('channels')
        if not isinstance(channels, dict):
            channels = {}
            doc['channels'] = channels
        feishu = channels.get('feishu')
        if not isinstance(feishu, dict):
            feishu = {}
            channels['feishu'] = feishu
        feishu['enabled'] = bool(enabled)
        self.save(doc)

    def set_feishu_credentials(self, app_id: str, app_secret: str) -> None:
        """写入扫码得到的飞书凭据，并打开通道开关。

        与 ``set_feishu_enabled`` 一样只走窄写入：不经过 UI 的整份配置，
        避免把界面上的占位密钥带进来。调用方必须先验证过凭据可用，
        本方法不做任何网络校验。
        """
        doc = copy.deepcopy(self.raw)
        channels = doc.get('channels')
        if not isinstance(channels, dict):
            channels = {}
            doc['channels'] = channels
        feishu = channels.get('feishu')
        if not isinstance(feishu, dict):
            feishu = {}
            channels['feishu'] = feishu
        feishu['appId'] = app_id
        feishu['appSecret'] = app_secret
        # 扫码的意图就是「连上」，重启后应自动建连
        feishu['enabled'] = True
        self.save(doc)

    def save(self, doc: dict[str, Any]) -> Config:
        """校验通过后原子写盘（0600）。保存不改变本进程正在使用的配置。"""
        if not isinstance(doc, dict):
            raise ConfigValidationError([_issue('', '配置必须是 JSON 对象')])
        restored = self.restore_secrets(doc)
        cfg, issues = self.validate_doc(restored)
        if issues or cfg is None:
            raise ConfigValidationError(issues or [_issue('', '配置不合法')])
        text = json.dumps(restored, ensure_ascii=False, indent=2) + '\n'
        save_or_create_file(self.path, text)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        # 只更新磁盘视图：本进程继续用启动时的 config，重启后才切换
        self.raw = restored
        self.status = 'ok'
        self.issues = []
        return cfg

    def reset(self) -> Path | None:
        """备份现有文件并重建可运行骨架；返回备份路径（无旧文件则 None）。"""
        backup = None
        if self.path.exists():
            stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
            backup = self.path.with_name(f'{self.path.name}.bak-{stamp}')
            self.path.replace(backup)
        self.load()
        self.save(self.raw)
        return backup


def main(argv: list[str] | None = None, *,
         data_paths: DataPaths | None = None) -> int:
    """``python -m zero.config``：默认只打印全量模板，``--write`` 写精简骨架。

    ``data_paths`` 由调用方提供；命令行入口用 ``DataPaths.default()``，
    测试传入隔离根。
    """
    parser = argparse.ArgumentParser(
        prog='python -m zero.config',
        description='打印配置模板；默认不写任何文件。')
    parser.add_argument(
        '--write', action='store_true',
        help='写入可运行的精简骨架（目标已存在时拒绝）')
    args = parser.parse_args(argv)

    if not args.write:
        print(render_template())
        return 0
    try:
        target = write_skeleton(
            (data_paths or DataPaths.default()).config)
    except ConfigWriteError as exc:
        print(f'未写入：{exc}', file=sys.stderr)
        return 1
    print(f'已写入 {target}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())


