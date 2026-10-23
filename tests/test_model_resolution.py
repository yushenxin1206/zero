"""Offline tests for Config.resolve_runtime: 三层配置（models/agentRuntimes/
providers）解析为 AgentRuntimeConfig（无真实 LLM）。
"""
import pytest

from zero.config import Config
from zero.models import (
    AgentRuntimeConfig,
    AgentRuntimeSpec,
    AgentRuntimeSpecs,
    CompactConfig,
    LLMCallOptions,
    LLMRuntime,
    MaintenanceConfig,
    ModelSpec,
    ProviderConfig,
    ProviderConfigs,
)

# 顶层区块（compact / maintenance）与共享 spec 的 token 预算默认值
COMPACT_DEFAULTS = {
    "enabled": True, "trigger_ratio": 0.75, "tail_ratio": 0.25,
    "context_compact_max_tokens": 2000,
}
MAINTENANCE_TOKEN_DEFAULTS = {
    "session_summary_max_tokens": 2000, "memory_max_tokens": 8000,
}
SPEC_HEADROOM_DEFAULT = 4096

MODELS = {
    "model-a": ModelSpec(provider="deepseek", context_window_tokens=11111),
    "model-b": ModelSpec(provider="dashscope", context_window_tokens=22222),
}

PROVIDERS = ProviderConfigs(
    deepseek=ProviderConfig(api_base="https://deepseek.local", api_key="dk"),
    dashscope=ProviderConfig(api_base="https://dashscope.local", api_key="ds"),
)


def cfg(runtimes, models=None, providers=None) -> Config:
    """构造一张可解析的根配置（解析只需 models/agentRuntimes/providers）。

    直接传 Pydantic 实例：revalidate_instances 默认 'never'，已构造的
    模型被原样保留，model_fields_set（显式性的唯一来源）不会被 model_dump
    的全字段往返污染。
    """
    return Config.model_validate({
        "models": models or MODELS,
        "agentRuntimes": runtimes,
        "providers": providers or PROVIDERS,
    })


def expect_value_error(config: Config, task: str, needle: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        config.resolve_runtime(task)
    assert needle in str(excinfo.value), str(excinfo.value)


# ---------------------------------------------------------------------------
# 任务级绑定 + 窗口 + 任务级显式字段
# ---------------------------------------------------------------------------

def test_task_binding():
    runtimes = AgentRuntimeSpecs(
        default=AgentRuntimeSpec(
            llm_call_options=LLMCallOptions(
                model="model-a", temperature=0.3, max_tokens=4096)),
        compact=AgentRuntimeSpec(
            llm_call_options=LLMCallOptions(temperature=0, max_tokens=777)),
    )
    rc = cfg(runtimes).resolve_runtime("default")
    assert isinstance(rc, AgentRuntimeConfig) and isinstance(rc, AgentRuntimeSpec)
    assert rc.llm_call_options.model == "model-a"
    assert rc.llm_call_options.temperature == 0.3       # 任务级显式字段
    assert rc.llm_call_options.max_tokens == 4096
    assert rc.llm_call_options.response_format == {"type": "text"}  # 非显式 → 默认
    assert rc.llm_runtime.provider.api_base == "https://deepseek.local"
    assert rc.llm_runtime.context_window_tokens == 11111
    assert rc.max_iterations == 50                      # 策略字段默认值


def test_fallback_to_default():
    """非 default 任务未绑模型 → 模型/窗口回退 default，自身显式参数仍生效。"""
    runtimes = AgentRuntimeSpecs(
        default=AgentRuntimeSpec(
            llm_call_options=LLMCallOptions(
                model="model-a", temperature=0.3, max_tokens=4096)),
        compact=AgentRuntimeSpec(
            llm_call_options=LLMCallOptions(temperature=0, max_tokens=777)),
    )
    rc_fb = cfg(runtimes).resolve_runtime("compact")
    assert rc_fb.llm_call_options.model == "model-a"
    assert rc_fb.llm_runtime.provider.api_base == "https://deepseek.local"
    assert rc_fb.llm_runtime.context_window_tokens == 11111
    assert rc_fb.llm_call_options.temperature == 0
    assert rc_fb.llm_call_options.max_tokens == 777
    # 未知任务名同样回退 default（配置文件的未知任务名已由 AgentRuntimeSpecs 拦截）
    rc_unknown = cfg(runtimes).resolve_runtime("missing")
    assert rc_unknown.llm_call_options.model == "model-a"


def test_llm_option_priority():
    """调用选项字段级优先级：task 显式 > default 显式 > 数据模型默认。"""
    merged = cfg(AgentRuntimeSpecs(
        default=AgentRuntimeSpec(llm_call_options=LLMCallOptions(
            model="model-a", temperature=0.2, max_tokens=4096)),
        compact=AgentRuntimeSpec(llm_call_options=LLMCallOptions(temperature=0)),
    )).resolve_runtime("compact")
    assert merged.llm_call_options.model == "model-a"    # 未绑 → 继承 default
    assert merged.llm_call_options.temperature == 0      # task 显式覆盖 default
    assert merged.llm_call_options.max_tokens == 4096    # 未显式 → 继承 default（非 8192）
    # 完全没有任务条目 → 整条继承 default
    inherited = cfg(AgentRuntimeSpecs(
        default=AgentRuntimeSpec(llm_call_options=LLMCallOptions(
            model="model-a", temperature=0.2, max_tokens=4096)),
    )).resolve_runtime("memory")
    assert inherited.llm_call_options.temperature == 0.2
    assert inherited.llm_call_options.max_tokens == 4096


def test_strategy_field_priority():
    """reasoning_token_headroom / max_iterations 走同一条优先级链。"""
    strat = cfg(AgentRuntimeSpecs(
        default=AgentRuntimeSpec(
            max_iterations=50, reasoning_token_headroom=2048,
            llm_call_options=LLMCallOptions(model="model-a")),
        compact=AgentRuntimeSpec(max_iterations=3),
    )).resolve_runtime("compact")
    assert strat.max_iterations == 3                     # task 显式覆盖 default
    assert strat.reasoning_token_headroom == 2048        # 继承 default 显式
    assert AgentRuntimeSpec().reasoning_token_headroom == SPEC_HEADROOM_DEFAULT


def test_alternate_task():
    rc3 = cfg(AgentRuntimeSpecs(
        default=AgentRuntimeSpec(llm_call_options=LLMCallOptions(model="model-a")),
        session_summary=AgentRuntimeSpec(
            llm_call_options=LLMCallOptions(model="model-b")),
    )).resolve_runtime("session_summary")
    assert rc3.llm_call_options.model == "model-b"
    assert rc3.llm_runtime.provider.api_base == "https://dashscope.local"
    assert rc3.llm_runtime.context_window_tokens == 22222
    assert rc3.llm_call_options.temperature == 0.1      # 无显式字段 → 数据模型默认


def test_top_level_sections():
    """compact / maintenance 区块承载压缩策略与输出上限。"""
    cc_default = CompactConfig()
    for k, v in COMPACT_DEFAULTS.items():
        assert getattr(cc_default, k) == v, (k, getattr(cc_default, k))
    mc_default = MaintenanceConfig()
    for k, v in MAINTENANCE_TOKEN_DEFAULTS.items():
        assert getattr(mc_default, k) == v, (k, getattr(mc_default, k))

    # Config 顶层字段：两个区块独立解析、camelCase 别名正常
    root = Config.model_validate({
        "models": MODELS,
        "providers": PROVIDERS,
        "compact": {"enabled": False, "triggerRatio": 0.5,
                    "tailRatio": 0.2, "contextCompactMaxTokens": 1500},
        "maintenance": {"sessionSummaryMaxTokens": 3000, "memoryMaxTokens": 9000},
    })
    assert root.compact.enabled is False and root.compact.trigger_ratio == 0.5
    assert root.compact.tail_ratio == 0.2
    assert root.compact.context_compact_max_tokens == 1500
    assert root.maintenance.session_summary_max_tokens == 3000
    assert root.maintenance.memory_max_tokens == 9000

    # 压缩输出上限 = 顶层 CompactConfig，与 compact 任务的调用选项无关
    rc4 = cfg(AgentRuntimeSpecs(
        default=AgentRuntimeSpec(llm_call_options=LLMCallOptions(model="model-a"))),
    ).resolve_runtime("compact")
    assert not hasattr(rc4, "context_compact_max_tokens")
    assert not hasattr(rc4, "context_compress_output_tokens")
    assert CompactConfig().context_compact_max_tokens == 2000

    cfg(AgentRuntimeSpecs(
        default=AgentRuntimeSpec(llm_call_options=LLMCallOptions(
            model="model-a", max_tokens=5000)),
        compact=AgentRuntimeSpec(llm_call_options=LLMCallOptions(temperature=0)),
    )).resolve_runtime("compact")
    assert CompactConfig().context_compact_max_tokens == 2000


def test_full_field_expansion_is_robust():
    """旧版全字段 dump 的配置：空串 model / null 可选字段视为未设置。"""
    expanded = LLMCallOptions.model_validate({
        "model": "", "toolChoice": None, "responseFormat": {"type": "text"},
        "stop": None, "stream": None, "streamOptions": None, "temperature": 0.1,
        "maxTokens": 100000, "reasoningEffort": None, "extraBody": None,
    })
    rc6 = cfg(AgentRuntimeSpecs(
        default=AgentRuntimeSpec(llm_call_options=LLMCallOptions(model="model-a")),
        compact=AgentRuntimeSpec(llm_call_options=expanded),
    )).resolve_runtime("compact")
    assert rc6.llm_call_options.model == "model-a"   # 空串 model 视为未绑定
    assert rc6.llm_call_options.temperature == 0.1
    assert rc6.llm_call_options.max_tokens == 100000
    assert rc6.llm_call_options.tool_choice is None  # null 不覆盖


def test_fail_fast():
    # 1. 完全未绑定（agentRuntimes 为空）
    expect_value_error(cfg(AgentRuntimeSpecs()), "default", "未绑定模型")
    # 2. default 有 llmCallOptions 但无 model，且无别处可回退
    expect_value_error(
        cfg(AgentRuntimeSpecs(default=AgentRuntimeSpec(
            llm_call_options=LLMCallOptions(temperature=0)))),
        "default", "未绑定模型")
    # 3. 绑定的模型不在 models 注册表
    expect_value_error(
        cfg(AgentRuntimeSpecs(default=AgentRuntimeSpec(
            llm_call_options=LLMCallOptions(model="nope")))),
        "default", "不在 models 注册表")
    # 4. provider 键未在 providers 中结算
    expect_value_error(
        cfg(AgentRuntimeSpecs(default=AgentRuntimeSpec(
            llm_call_options=LLMCallOptions(model="model-x"))),
            models={"model-x": ModelSpec(
                provider="openai", context_window_tokens=1)}),
        "default", "未在 providers 中配置")


def test_extra_forbid():
    # 旧键 llmConfig 立即报错
    with pytest.raises(ValueError) as excinfo:
        AgentRuntimeSpec.model_validate({"llmConfig": {"model": "x"}})
    assert "extra" in str(excinfo.value).lower()
    # 共享 spec 字段与 llmCallOptions 共存，camelCase 别名正常
    s = AgentRuntimeSpec.model_validate(
        {"maxIterations": 7, "reasoningTokenHeadroom": 2048,
         "llmCallOptions": {"model": "x"}})
    assert s.max_iterations == 7 and s.reasoning_token_headroom == 2048
    assert s.llm_call_options is not None and s.llm_call_options.model == "x"
    # 已迁出的压缩策略键触发 extra 报错
    with pytest.raises(ValueError) as excinfo:
        AgentRuntimeSpec.model_validate({"contextCompressEnabled": False})
    assert "extra" in str(excinfo.value).lower()
    # 未知任务名立即报错
    with pytest.raises(ValueError) as excinfo:
        AgentRuntimeSpecs.model_validate({"unknown_task": {}})
    assert "extra" in str(excinfo.value).lower()


def test_type_consistency():
    assert AgentRuntimeSpec().llm_call_options == LLMCallOptions()
    assert AgentRuntimeSpec().llm_call_options.model == ''
    assert isinstance(AgentRuntimeConfig().llm_call_options, LLMCallOptions)
    assert isinstance(AgentRuntimeConfig().llm_runtime, LLMRuntime)
    config_like = AgentRuntimeConfig(
        llm_call_options=LLMCallOptions(model="m"),
        llm_runtime=LLMRuntime(provider=PROVIDERS.deepseek, context_window_tokens=999),
        max_iterations=3,
        reasoning_token_headroom=2048,
    )
    assert config_like.llm_runtime.provider is PROVIDERS.deepseek
    assert config_like.llm_runtime.context_window_tokens == 999
    assert config_like.max_iterations == 3
