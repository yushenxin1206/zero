"""P1-3 验收：配置模板不覆盖手写文件，继承关系不被拍平。"""
import json
from pathlib import Path

from tests.support import current

from zero import config as config_module
from zero.config import Config, load_config, render_skeleton, render_template

# 精简配置：compact 只覆盖 temperature，其余字段靠继承 default / 模型默认值
MINIMAL = {
    "models": {"m1": {"provider": "deepseek", "contextWindowTokens": 65536}},
    "providers": {"deepseek": {"apiBase": "https://test.local", "apiKey": "k"}},
    "agentRuntimes": {
        "default": {"llmCallOptions": {"model": "m1"}},
        "compact": {"llmCallOptions": {"temperature": 0.9}},
    },
}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _snapshot(path: Path) -> tuple[str, int]:
    return path.read_text(encoding="utf-8"), path.stat().st_mtime_ns


def test_template_ignores_environment(monkeypatch):
    """模板只含字段默认值：环境变量不能混进模板（否则 --write 会把它固化）。"""
    monkeypatch.setenv(
        "MODELS",
        json.dumps({"env-m": {"provider": "deepseek", "contextWindowTokens": 1}}),
    )
    assert Config().models, "环境变量确实会被 settings 读进来"
    assert json.loads(render_template())["models"] == {}


def test_template_is_a_valid_config():
    """模板必须能重新通过校验：未来新增必填字段或校验器会在这里变红。"""
    template = json.loads(render_template())
    Config.model_validate(template)
    # 默认打印的是全量参考（包含派生的 compact 条目），与 --write 的骨架不同
    assert "compact" in template["agentRuntimes"]


def test_template_command_never_touches_existing_config(capsys):
    """已有配置时：默认只打印，--write 也拒绝，字节与 mtime 都不变。"""
    target = current().config
    _write(target, MINIMAL)
    before = _snapshot(target)

    assert config_module.main([]) == 0
    assert "agentRuntimes" in capsys.readouterr().out
    assert _snapshot(target) == before

    assert config_module.main(["--write"], data_paths=current()) == 1
    assert "已存在" in capsys.readouterr().err
    assert _snapshot(target) == before


def test_template_write_creates_runnable_skeleton():
    """--write 落盘的是精简骨架：结构完整、能解析并绑定模型。"""
    target = current().config
    assert not target.exists()

    assert config_module.main(["--write"], data_paths=current()) == 0
    assert target.exists()
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written == json.loads(render_skeleton())

    loaded = load_config(target)
    default_runtime = loaded.resolve_runtime("default")
    assert default_runtime.llm_call_options.model
    # 派生任务不需要出现在文件里也能解析出同一个模型
    assert (loaded.resolve_runtime("compact").llm_call_options.model
            == default_runtime.llm_call_options.model)


def test_skeleton_leaves_derived_tasks_to_inheritance():
    """骨架不写死派生任务；改 default 后 compact 等全部跟随继承。"""
    skeleton = json.loads(render_skeleton())
    assert set(skeleton["agentRuntimes"]) == {"default"}

    data = json.loads(json.dumps(skeleton))
    data["agentRuntimes"]["default"]["llmCallOptions"].update(
        {"maxTokens": 1234, "temperature": 0.2})
    _write(current().config, data)

    loaded = load_config(current().config)
    for task in ("compact", "session_summary", "memory", "subagent"):
        runtime = loaded.resolve_runtime(task)
        assert runtime.llm_call_options.model == "deepseek-chat"
        assert runtime.llm_call_options.max_tokens == 1234
        assert runtime.llm_call_options.temperature == 0.2


def test_load_config_keeps_explicit_field_set():
    """精简配置读回来仍然只有手写的那几个键，继承语义没被补全。"""
    target = current().config
    _write(target, MINIMAL)
    before = _snapshot(target)

    loaded = load_config(target)
    assert loaded.model_dump(mode="json", by_alias=True, exclude_unset=True) == MINIMAL
    # 读路径零写盘
    assert _snapshot(target) == before


def test_compact_inherits_default_max_tokens():
    """compact 只写 temperature：default 的 maxTokens 改了，compact 跟着变。"""
    data = json.loads(json.dumps(MINIMAL))
    data["agentRuntimes"]["default"]["llmCallOptions"]["maxTokens"] = 1234
    _write(current().config, data)

    runtime = load_config(current().config).resolve_runtime("compact")
    assert runtime.llm_call_options.temperature == 0.9
    assert runtime.llm_call_options.max_tokens == 1234
