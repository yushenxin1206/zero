"""Offline tests for model switching: resolve_runtime / resolve_task_llm /
SessionStore.get_model·set_model / Config 三层结构（无真实 LLM、隔离存储）。

Run: python tests/test_model_resolution.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import zero.session.store as store_mod
from zero.models import (
    AgentRuntimeConfig,
    AgentRuntimeSpec,
    LLMConfig,
    ModelResolver,
    ModelSpec,
    ProviderConfig,
    ProviderConfigs,
    TaskConfig,
    TaskConfigs,
)
resolve_runtime = ModelResolver.resolve_runtime
resolve_task_llm = ModelResolver.resolve_task_llm
DEFAULT_COMPACT_OUTPUT_TOKENS = ModelResolver.DEFAULT_COMPACT_OUTPUT_TOKENS
from zero.session.store import SessionStore

# Isolate storage: redirect SESSIONS_ROOT to a temp dir for this test run.
_tmp = tempfile.TemporaryDirectory()
store_mod.SESSIONS_ROOT = Path(_tmp.name)

CHANNEL = "cli"

MODELS = {
    "model-a": ModelSpec(provider="deepseek", context_window_tokens=11111),
    "model-b": ModelSpec(provider="dashscope", context_window_tokens=22222),
}

PROVIDERS = ProviderConfigs(
    deepseek=ProviderConfig(api_base="https://deepseek.local", api_key="dk"),
    dashscope=ProviderConfig(api_base="https://dashscope.local", api_key="ds"),
)


def spec(**kw) -> AgentRuntimeSpec:
    return AgentRuntimeSpec(**kw)


# --- resolve_runtime: 任务级绑定 + 窗口 + 任务级显式字段 --------------------------
tasks = TaskConfigs(
    default=TaskConfig(
        llm_config=LLMConfig(model="model-a", temperature=0.3, max_tokens=4096)),
    compact=TaskConfig(
        llm_config=LLMConfig(temperature=0, max_tokens=777)),
)
rc = resolve_runtime("default", spec(), tasks, MODELS, PROVIDERS)
assert isinstance(rc, AgentRuntimeConfig) and isinstance(rc, AgentRuntimeSpec)
assert rc.provider is PROVIDERS.deepseek
assert rc.llm_config.model == "model-a"
assert rc.llm_config.temperature == 0.3       # 任务级显式字段
assert rc.llm_config.max_tokens == 4096
assert rc.llm_config.response_format == {"type": "text"}  # 非显式字段继承基座默认
assert rc.context_window_tokens == 11111
assert rc.max_iterations == 50                 # spec 策略字段
assert rc.context_compress_output_tokens == 777  # tasks.compact.llmConfig.maxTokens 回填
print("resolve_runtime task binding ok")

# --- resolve_runtime: 会话级覆盖（/model 切换）---------------------------------
rc2 = resolve_runtime("default", spec(), tasks, MODELS, PROVIDERS,
                      session_model="model-b")
assert rc2.llm_config.model == "model-b"       # 任务绑定 model-a 不盖会话覆盖
assert rc2.provider is PROVIDERS.dashscope
assert rc2.context_window_tokens == 22222
# 覆盖只换模型，任务级显式参数仍生效
assert rc2.llm_config.temperature == 0.3
assert rc2.llm_config.max_tokens == 4096
print("resolve_runtime session override ok")

# --- resolve_runtime: 换 task ---------------------------------------------------
rc3 = resolve_runtime("session_summary", spec(),
                      TaskConfigs(session_summary=TaskConfig(llm_config=LLMConfig(model="model-a"))),
                      MODELS, PROVIDERS)
assert rc3.llm_config.model == "model-a"
assert rc3.llm_config.temperature == 0.1      # 无显式字段 → LLMConfig 默认
print("resolve_runtime alternate task ok")

# --- resolve_runtime: compact 输出上限回填的显式语义 ----------------------------
# compact 任务未配置 → 默认
rc4 = resolve_runtime("default", spec(),
                      TaskConfigs(default=TaskConfig(llm_config=LLMConfig(model="model-a"))),
                      MODELS, PROVIDERS)
assert rc4.context_compress_output_tokens == DEFAULT_COMPACT_OUTPUT_TOKENS == 2000
# compact 配了 llmConfig 但未写 maxTokens → 不把默认值 8192 当上限
rc5 = resolve_runtime(
    "default", spec(),
    TaskConfigs(
        default=TaskConfig(llm_config=LLMConfig(model="model-a")),
        compact=TaskConfig(llm_config=LLMConfig(temperature=0))),
    MODELS, PROVIDERS)
assert rc5.context_compress_output_tokens == 2000
print("resolve_runtime compact output tokens ok")

# --- 任务级 llm_config 对全字段展开旧配置的健壮性 ---------------------------------
# 旧版 save_config 全字段落盘：磁盘 llmConfig 会带 "model": ""、null 的
# 可选字段与默认值。合并必须认 model_fields_set 且 model 一律不覆盖基座、
# null 视为未设置，否则空串 model 会盖掉基座（会话）绑定的模型名。
expanded = LLMConfig.model_validate({
    "model": "", "toolChoice": None, "responseFormat": {"type": "text"},
    "stop": None, "stream": None, "streamOptions": None, "temperature": 0.1,
    "maxTokens": 100000, "reasoningEffort": None, "extraBody": None,
})
tasks_expanded = TaskConfigs(
    default=TaskConfig(llm_config=expanded),
    compact=TaskConfig(llm_config=expanded),
)
rc6 = resolve_runtime("default", spec(), tasks_expanded, MODELS, PROVIDERS,
                      session_model="model-a")
assert rc6.llm_config.model == "model-a"   # 空串 model 不覆盖基座绑定
assert rc6.llm_config.temperature == 0.1
assert rc6.llm_config.max_tokens == 100000
assert rc6.llm_config.tool_choice is None  # null 不覆盖
assert rc6.context_compress_output_tokens == 100000
print("llm_config robust to full-field expansion ok")

# --- resolve_runtime: fail-fast --------------------------------------------------
for bad_kwargs, hint in [
    (dict(task="default", spec=spec(), tasks=TaskConfigs(),
          models=MODELS, provider_configs=PROVIDERS), "未绑定模型"),
    (dict(task="default", spec=spec(),
          tasks=TaskConfigs(default=TaskConfig(llm_config=LLMConfig(temperature=0))),
          models=MODELS, provider_configs=PROVIDERS), "未绑定模型"),
    (dict(task="missing", spec=spec(),
          tasks=TaskConfigs(default=TaskConfig(llm_config=LLMConfig(model="model-a"))),
          models=MODELS, provider_configs=PROVIDERS), "未绑定模型"),
    (dict(task="default", spec=spec(),
          tasks=TaskConfigs(default=TaskConfig(llm_config=LLMConfig(model="nope"))),
          models=MODELS, provider_configs=PROVIDERS), "不在 models 注册表"),
    (dict(task="default", spec=spec(),
          tasks=TaskConfigs(default=TaskConfig(llm_config=LLMConfig(model="model-a"))),
          models=MODELS, provider_configs=PROVIDERS, session_model="nope"),
     "不在 models 注册表"),
]:
    try:
        resolve_runtime(**bad_kwargs)
    except ValueError as e:
        assert hint in str(e), (hint, str(e))
    else:
        raise AssertionError(f"expected ValueError ({hint})")
print("resolve_runtime fail-fast ok")

# --- AgentRuntimeSpec: extra='forbid'（遗留内联字段立即报错）---------------------
try:
    AgentRuntimeSpec.model_validate(
        {"provider": "deepseek", "llmConfig": {"model": "x"}})
except ValueError as e:
    assert "provider" in str(e) and "extra" in str(e).lower()
else:
    raise AssertionError("expected ValidationError for legacy inline fields")
# camelCase 别名正常
s = AgentRuntimeSpec.model_validate(
    {"maxIterations": 7, "contextCompressEnabled": False})
assert s.max_iterations == 7 and s.context_compress_enabled is False
print("AgentRuntimeSpec extra=forbid ok")

# --- 存量构造兼容：AgentRuntimeConfig 继承后旧 kwargs 仍合法 ----------------------
legacy = AgentRuntimeConfig(
    provider=PROVIDERS.deepseek, llm_config=LLMConfig(model="m"),
    context_window_tokens=999, max_iterations=3,
    context_compress_enabled=True,
    context_compress_trigger_ratio=0.5,
    context_compress_tail_ratio=0.2,
    context_compress_output_tokens=100,
)
assert legacy.provider is PROVIDERS.deepseek and legacy.max_iterations == 3
assert legacy.context_compress_output_tokens == 100
print("legacy AgentRuntimeConfig kwargs ok")

# --- resolve_task_llm: 未绑定 → 跟随 fallback（主对话有效配置）-------------------
c = resolve_task_llm("compact", tasks, MODELS, PROVIDERS, rc2)
assert c.provider is PROVIDERS.dashscope     # fallback = 主对话（含会话覆盖）
assert c.llm_config.model == "model-b"
assert c.llm_config.temperature == 0          # compact 任务显式字段
assert c.llm_config.max_tokens == 777
print("resolve_task_llm follow fallback ok")

# --- resolve_task_llm: 显式绑定模型 → 独立基座 + 任务级显式字段 -------------------
tasks2 = TaskConfigs(
    default=TaskConfig(llm_config=LLMConfig(model="model-a")),
    compact=TaskConfig(llm_config=LLMConfig(model="model-b", temperature=0)),
    memory=TaskConfig(),
)
c2 = resolve_task_llm("compact", tasks2, MODELS, PROVIDERS, rc)
assert c2.provider is PROVIDERS.dashscope and c2.llm_config.model == "model-b"
assert c2.llm_config.temperature == 0
assert c2.llm_config.max_tokens == 8192        # 基座 = LLMConfig 默认（未显式写）
c3 = resolve_task_llm("memory", tasks2, MODELS, PROVIDERS, rc)  # 任务条目存在但未绑模型
assert c3.llm_config.model == rc.llm_config.model
print("resolve_task_llm explicit binding ok")

# --- resolve_task_llm: fail-fast --------------------------------------------------
try:
    resolve_task_llm("compact",
                     TaskConfigs(compact=TaskConfig(llm_config=LLMConfig(model="nope"))),
                     MODELS, PROVIDERS, rc)
except ValueError as e:
    assert "不在 models 注册表" in str(e)
else:
    raise AssertionError("expected ValueError")
print("resolve_task_llm fail-fast ok")

# --- SessionStore.get_model / set_model -------------------------------------------
sid = SessionStore.create_session(CHANNEL, "src-model")
assert SessionStore.get_model(CHANNEL, sid) == ""
assert SessionStore.set_model(CHANNEL, sid, "model-b") is True
assert SessionStore.get_model(CHANNEL, sid) == "model-b"
# set_model("") 清除覆盖 → 恢复跟随默认
assert SessionStore.set_model(CHANNEL, sid, "") is True
assert SessionStore.get_model(CHANNEL, sid) == ""
assert SessionStore.set_model(CHANNEL, "no-such-session", "x") is False
# 索引往返：model 字段落盘后可读回
sid2 = SessionStore.create_session(CHANNEL, "src-model-2")
SessionStore.set_model(CHANNEL, sid2, "model-a")
metas = {m.session_id: m for m in SessionStore.list_sessions(CHANNEL)}
assert metas[sid2].model == "model-a"
print("SessionStore get/set_model ok")

# --- 集成：会话覆盖驱动 resolve 链 --------------------------------------------------
sid3 = SessionStore.create_session(CHANNEL, "src-model-3")
base = resolve_runtime("default", spec(), tasks, MODELS, PROVIDERS,
                       SessionStore.get_model(CHANNEL, sid3))
assert base.llm_config.model == "model-a"
SessionStore.set_model(CHANNEL, sid3, "model-b")
switched = resolve_runtime("default", spec(), tasks, MODELS, PROVIDERS,
                           SessionStore.get_model(CHANNEL, sid3))
assert switched.llm_config.model == "model-b"
assert switched.provider is PROVIDERS.dashscope
print("session override drives resolve ok")

print("ALL MODEL RESOLUTION TESTS PASSED")
