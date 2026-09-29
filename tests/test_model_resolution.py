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
    DEFAULT_COMPACT_OUTPUT_TOKENS,
    AgentRuntimeConfig,
    AgentRuntimeSpec,
    LLMConfig,
    ModelSpec,
    TaskConfig,
    resolve_runtime,
    resolve_task_llm,
)
from zero.session.store import SessionStore

# Isolate storage: redirect SESSIONS_ROOT to a temp dir for this test run.
_tmp = tempfile.TemporaryDirectory()
store_mod.SESSIONS_ROOT = Path(_tmp.name)

CHANNEL = "cli"

MODELS = {
    "model-a": ModelSpec(provider="prov-a", context_window_tokens=11111),
    "model-b": ModelSpec(provider="prov-b", context_window_tokens=22222),
}


def spec(**kw) -> AgentRuntimeSpec:
    return AgentRuntimeSpec(**kw)


# --- resolve_runtime: 任务级绑定 + 窗口 + overlay -------------------------------
tasks = {
    "default": TaskConfig(
        model="model-a",
        llm_config=LLMConfig(temperature=0.3, max_tokens=4096)),
    "compact": TaskConfig(
        llm_config=LLMConfig(temperature=0, max_tokens=777)),
}
rc = resolve_runtime(spec(), tasks, MODELS)
assert isinstance(rc, AgentRuntimeConfig) and isinstance(rc, AgentRuntimeSpec)
assert rc.provider == "prov-a"
assert rc.llm_config.model == "model-a"
assert rc.llm_config.temperature == 0.3       # overlay 显式字段
assert rc.llm_config.max_tokens == 4096
assert rc.llm_config.response_format == {"type": "text"}  # 非显式字段继承基座默认
assert rc.context_window_tokens == 11111
assert rc.max_iterations == 50                 # spec 策略字段
assert rc.task == "default"
assert rc.context_compress_output_tokens == 777  # tasks.compact.llmConfig.maxTokens 回填
print("resolve_runtime task binding ok")

# --- resolve_runtime: 会话级覆盖（/model 切换）---------------------------------
rc2 = resolve_runtime(spec(), tasks, MODELS, session_model="model-b")
assert rc2.llm_config.model == "model-b"
assert rc2.provider == "prov-b"
assert rc2.context_window_tokens == 22222
# 覆盖只换模型，任务级 overlay 仍生效
assert rc2.llm_config.temperature == 0.3
assert rc2.llm_config.max_tokens == 4096
print("resolve_runtime session override ok")

# --- resolve_runtime: 换 task ---------------------------------------------------
rc3 = resolve_runtime(spec(task="other"), {"other": TaskConfig(model="model-b")}, MODELS)
assert rc3.llm_config.model == "model-b"
assert rc3.llm_config.temperature == 0.1      # 无 overlay → LLMConfig 默认
print("resolve_runtime alternate task ok")

# --- resolve_runtime: compact 输出上限回填的显式语义 ----------------------------
# compact 任务未配置 → 默认
rc4 = resolve_runtime(spec(), {"default": TaskConfig(model="model-a")}, MODELS)
assert rc4.context_compress_output_tokens == DEFAULT_COMPACT_OUTPUT_TOKENS == 2000
# compact 配了 llmConfig 但未写 maxTokens → 不把 LLMConfig 字段默认 8192 当上限
rc5 = resolve_runtime(
    spec(),
    {"default": TaskConfig(model="model-a"),
     "compact": TaskConfig(llm_config=LLMConfig(temperature=0))},
    MODELS)
assert rc5.context_compress_output_tokens == 2000
print("resolve_runtime compact output tokens ok")

# --- resolve_runtime: fail-fast --------------------------------------------------
for bad_kwargs, hint in [
    (dict(spec=spec(), tasks={}, models=MODELS), "未绑定模型"),
    (dict(spec=spec(task="missing"),
          tasks={"default": TaskConfig(model="model-a")}, models=MODELS), "未绑定模型"),
    (dict(spec=spec(), tasks={"default": TaskConfig(model="nope")}, models=MODELS), "不在 models 注册表"),
    (dict(spec=spec(), tasks={"default": TaskConfig(model="model-a")},
          models=MODELS, session_model="nope"), "不在 models 注册表"),
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
        {"task": "default", "provider": "deepseek", "llmConfig": {"model": "x"}})
except ValueError as e:
    assert "provider" in str(e) and "extra" in str(e).lower()
else:
    raise AssertionError("expected ValidationError for legacy inline fields")
# camelCase 别名正常
s = AgentRuntimeSpec.model_validate(
    {"task": "t", "maxIterations": 7, "contextCompressEnabled": False})
assert s.max_iterations == 7 and s.context_compress_enabled is False
print("AgentRuntimeSpec extra=forbid ok")

# --- 存量构造兼容：AgentRuntimeConfig 继承后旧 kwargs 仍合法 ----------------------
legacy = AgentRuntimeConfig(
    provider="p", llm_config=LLMConfig(model="m"),
    context_window_tokens=999, max_iterations=3,
    context_compress_enabled=True,
    context_compress_trigger_ratio=0.5,
    context_compress_tail_ratio=0.2,
    context_compress_output_tokens=100,
)
assert legacy.provider == "p" and legacy.max_iterations == 3
assert legacy.context_compress_output_tokens == 100
print("legacy AgentRuntimeConfig kwargs ok")

# --- resolve_task_llm: 未绑定 → 跟随 fallback（主对话有效配置）-------------------
c = resolve_task_llm("compact", tasks, MODELS, rc2)
assert c.provider == "prov-b"                 # fallback = 主对话（含会话覆盖）
assert c.llm_config.model == "model-b"
assert c.llm_config.temperature == 0          # compact 任务 overlay
assert c.llm_config.max_tokens == 777
print("resolve_task_llm follow fallback ok")

# --- resolve_task_llm: 显式绑定模型 → 独立基座 + overlay --------------------------
tasks2 = {
    "default": TaskConfig(model="model-a"),
    "compact": TaskConfig(model="model-b", llm_config=LLMConfig(temperature=0)),
    "memory": TaskConfig(),
}
c2 = resolve_task_llm("compact", tasks2, MODELS, rc)
assert c2.provider == "prov-b" and c2.llm_config.model == "model-b"
assert c2.llm_config.temperature == 0
assert c2.llm_config.max_tokens == 8192        # 基座 = LLMConfig 默认（overlay 未写）
c3 = resolve_task_llm("memory", tasks2, MODELS, rc)  # 任务条目存在但未绑模型
assert c3.llm_config.model == rc.llm_config.model
print("resolve_task_llm explicit binding ok")

# --- resolve_task_llm: fail-fast --------------------------------------------------
try:
    resolve_task_llm("compact", {"compact": TaskConfig(model="nope")}, MODELS, rc)
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
base = resolve_runtime(spec(), tasks, MODELS,
                       SessionStore.get_model(CHANNEL, sid3))
assert base.llm_config.model == "model-a"
SessionStore.set_model(CHANNEL, sid3, "model-b")
switched = resolve_runtime(spec(), tasks, MODELS,
                           SessionStore.get_model(CHANNEL, sid3))
assert switched.llm_config.model == "model-b"
assert switched.provider == "prov-b"
print("session override drives resolve ok")

print("ALL MODEL RESOLUTION TESTS PASSED")
