"""Offline tests for Config.resolve_runtime: 三层配置（models/agentRuntimes/
providers）解析为 AgentRuntimeConfig（无真实 LLM）。

Run: python tests/test_model_resolution.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zero.config import Config
from zero.models import (
    AgentRuntimeConfig,
    AgentRuntimeSpec,
    AgentRuntimeSpecs,
    LLMCallOptions,
    LLMRuntime,
    ModelSpec,
    ProviderConfig,
    ProviderConfigs,
)

# 未显式配置 maxTokens 时，压缩输出上限就是 LLMCallOptions 的默认 max_tokens
COMPACT_OUTPUT_FALLBACK = LLMCallOptions().max_tokens

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


# --- resolve_runtime: 任务级绑定 + 窗口 + 任务级显式字段 --------------------------
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
print("resolve_runtime task binding ok")

# --- resolve_runtime: 非 default 任务未绑模型 → 回退 default 绑定 ------------------
# compact 未绑 model（只有调用参数）→ 模型回退 default 的 model-a，
# llm_runtime 随之来自 model-a；compact 自己的显式参数仍生效
rc_fb = cfg(runtimes).resolve_runtime("compact")
assert rc_fb.llm_call_options.model == "model-a"
assert rc_fb.llm_runtime.provider.api_base == "https://deepseek.local"
assert rc_fb.llm_runtime.context_window_tokens == 11111
assert rc_fb.llm_call_options.temperature == 0
assert rc_fb.llm_call_options.max_tokens == 777
# 未知任务名同样回退 default（配置文件的未知任务名已由 AgentRuntimeSpecs 拦截）
rc_unknown = cfg(runtimes).resolve_runtime("missing")
assert rc_unknown.llm_call_options.model == "model-a"
print("resolve_runtime fallback to default ok")

# --- resolve_runtime: 调用选项字段级优先级 task > default > 数据模型默认 ------------
# default 显式设了 temperature/maxTokens，compact 只显式设 temperature →
# 只覆盖 temperature，其余字段（含未显式写的 model）继承 default
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
print("resolve_runtime llm option priority ok")

# --- resolve_runtime: 策略字段走同一条优先级链（合并 agentRuntime 后的新能力）-------
strat = cfg(AgentRuntimeSpecs(
    default=AgentRuntimeSpec(
        max_iterations=50, context_compress_trigger_ratio=0.5,
        llm_call_options=LLMCallOptions(model="model-a")),
    compact=AgentRuntimeSpec(max_iterations=3),
)).resolve_runtime("compact")
assert strat.max_iterations == 3                     # task 显式覆盖 default
assert strat.context_compress_trigger_ratio == 0.5    # 继承 default 显式
assert strat.context_compress_tail_ratio == 0.25      # 两层都没写 → 数据模型默认
assert strat.context_compress_enabled is True
print("resolve_runtime strategy field priority ok")

# --- resolve_runtime: 换 task ---------------------------------------------------
rc3 = cfg(AgentRuntimeSpecs(
    default=AgentRuntimeSpec(llm_call_options=LLMCallOptions(model="model-a")),
    session_summary=AgentRuntimeSpec(
        llm_call_options=LLMCallOptions(model="model-b")),
)).resolve_runtime("session_summary")
assert rc3.llm_call_options.model == "model-b"
assert rc3.llm_runtime.provider.api_base == "https://dashscope.local"
assert rc3.llm_runtime.context_window_tokens == 22222
assert rc3.llm_call_options.temperature == 0.1      # 无显式字段 → 数据模型默认
print("resolve_runtime alternate task ok")

# --- resolve_runtime: 压缩输出上限 = compact 解析后的 max_tokens ------------------
# compact 未显式写 maxTokens（default 也没写）→ 落数据模型默认
rc4 = cfg(AgentRuntimeSpecs(
    default=AgentRuntimeSpec(llm_call_options=LLMCallOptions(model="model-a"))),
).resolve_runtime("compact")
assert rc4.llm_call_options.max_tokens == COMPACT_OUTPUT_FALLBACK
# default 写了 maxTokens、compact 只写 temperature → 上限继承 default
rc5 = cfg(AgentRuntimeSpecs(
    default=AgentRuntimeSpec(
        llm_call_options=LLMCallOptions(model="model-a", max_tokens=5000)),
    compact=AgentRuntimeSpec(llm_call_options=LLMCallOptions(temperature=0)),
)).resolve_runtime("compact")
assert rc5.llm_call_options.max_tokens == 5000
# AgentRuntimeConfig 上不再有独立字段承载它
assert not hasattr(rc5, "context_compress_output_tokens")
print("compact output tokens come from compact max_tokens ok")

# --- 条目对全字段展开旧配置的健壮性 ----------------------------------------------
# 旧版 save_config 全字段落盘：磁盘 llmCallOptions 会带 "model": ""、null 的
# 可选字段与默认值。合并按 exclude_unset 取显式键、空串/null 视为未设置，
# 否则空串 model 会盖掉从 default 继承的绑定模型名。
expanded = LLMCallOptions.model_validate({
    "model": "", "toolChoice": None, "responseFormat": {"type": "text"},
    "stop": None, "stream": None, "streamOptions": None, "temperature": 0.1,
    "maxTokens": 100000, "reasoningEffort": None, "extraBody": None,
})
rc6 = cfg(AgentRuntimeSpecs(
    default=AgentRuntimeSpec(llm_call_options=LLMCallOptions(model="model-a")),
    compact=AgentRuntimeSpec(llm_call_options=expanded),
)).resolve_runtime("compact")
assert rc6.llm_call_options.model == "model-a"   # 空串 model 视为未绑定 → 回退 default
assert rc6.llm_call_options.temperature == 0.1
assert rc6.llm_call_options.max_tokens == 100000
assert rc6.llm_call_options.tool_choice is None  # null 不覆盖
print("llm_call_options robust to full-field expansion ok")

# --- resolve_runtime: fail-fast --------------------------------------------------
# 1. 完全未绑定（agentRuntimes 为空）
try:
    cfg(AgentRuntimeSpecs()).resolve_runtime("default")
except ValueError as e:
    assert "未绑定模型" in str(e), str(e)
else:
    raise AssertionError("expected ValueError (未绑定模型)")
# 2. default 有 llmCallOptions 但无 model，且无别处可回退
try:
    cfg(AgentRuntimeSpecs(
        default=AgentRuntimeSpec(llm_call_options=LLMCallOptions(temperature=0))),
    ).resolve_runtime("default")
except ValueError as e:
    assert "未绑定模型" in str(e), str(e)
else:
    raise AssertionError("expected ValueError (未绑定模型)")
# 3. 绑定的模型不在 models 注册表
try:
    cfg(AgentRuntimeSpecs(
        default=AgentRuntimeSpec(llm_call_options=LLMCallOptions(model="nope"))),
    ).resolve_runtime("default")
except ValueError as e:
    assert "不在 models 注册表" in str(e), str(e)
else:
    raise AssertionError("expected ValueError (不在 models 注册表)")
# 4. provider 键未在 providers 中结算
try:
    cfg(
        AgentRuntimeSpecs(default=AgentRuntimeSpec(
            llm_call_options=LLMCallOptions(model="model-x"))),
        models={"model-x": ModelSpec(provider="openai", context_window_tokens=1)},
    ).resolve_runtime("default")
except ValueError as e:
    assert "未在 providers 中配置" in str(e), str(e)
else:
    raise AssertionError("expected ValueError (provider 未配置)")
print("resolve_runtime fail-fast ok")

# --- AgentRuntimeSpec: extra='forbid'（未知键立即报错，camelCase 别名正常）--------
try:
    AgentRuntimeSpec.model_validate({"llmConfig": {"model": "x"}})
except ValueError as e:
    assert "extra" in str(e).lower()
else:
    raise AssertionError("expected ValidationError for legacy llmConfig key")
# 策略字段与 llmCallOptions 共存，camelCase 别名正常
s = AgentRuntimeSpec.model_validate(
    {"maxIterations": 7, "contextCompressEnabled": False,
     "llmCallOptions": {"model": "x"}})
assert s.max_iterations == 7 and s.context_compress_enabled is False
assert s.llm_call_options is not None and s.llm_call_options.model == "x"
print("AgentRuntimeSpec extra=forbid ok")

# --- AgentRuntimeSpecs: extra='forbid'（未知任务名立即报错）------------------------
try:
    AgentRuntimeSpecs.model_validate({"unknown_task": {}})
except ValueError as e:
    assert "extra" in str(e).lower()
else:
    raise AssertionError("expected ValidationError for unknown task name")
print("AgentRuntimeSpecs extra=forbid ok")

# --- 类型收窄：Spec 里 llm_call_options 可空，Config 里非空 ------------------------
assert AgentRuntimeSpec().llm_call_options is None
assert isinstance(AgentRuntimeConfig().llm_call_options, LLMCallOptions)
assert isinstance(AgentRuntimeConfig().llm_runtime, LLMRuntime)
config_like = AgentRuntimeConfig(
    llm_call_options=LLMCallOptions(model="m"),
    llm_runtime=LLMRuntime(provider=PROVIDERS.deepseek, context_window_tokens=999),
    max_iterations=3,
    context_compress_enabled=True,
    context_compress_trigger_ratio=0.5,
    context_compress_tail_ratio=0.2,
)
assert config_like.llm_runtime.provider is PROVIDERS.deepseek
assert config_like.llm_runtime.context_window_tokens == 999
assert config_like.max_iterations == 3
print("AgentRuntimeConfig narrowing ok")

print("ALL MODEL RESOLUTION TESTS PASSED")