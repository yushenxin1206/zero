"""配置读写入口：首次引导判定、字段级校验、密钥掩码与原子写。"""
import asyncio
import json
import stat

import pytest

from tests.support import current

from zero.config import (
    MASKED_SECRET,
    Config,
    ConfigManager,
    ConfigValidationError,
    render_skeleton,
)
from zero.maintenance.engine import MaintenanceEngine

MINIMAL = {
    "models": {"m1": {"provider": "deepseek", "contextWindowTokens": 65536}},
    "providers": {"deepseek": {"apiBase": "https://test.local", "apiKey": "sk-1"}},
    "agentRuntimes": {"default": {"llmCallOptions": {"model": "m1"}}},
}


def _write(data) -> None:
    path = current().config
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_missing_file_requires_setup():
    """没有 config.json：判定 missing，且读取本身不写盘。"""
    manager = ConfigManager(current().config)
    manager.load()

    assert manager.needs_setup
    payload = manager.setup_payload()
    assert payload["required"] is True
    assert payload["reason"] == "missing"
    assert payload["configPath"] == str(current().config)
    assert not current().config.exists()
    # 下发给前端的草稿是可直接保存的骨架
    assert json.loads(render_skeleton()) == manager.masked_doc()


def test_save_writes_runnable_config_with_private_permissions():
    manager = ConfigManager(current().config)
    manager.load()

    cfg = manager.save(MINIMAL)

    assert current().config.exists()
    mode = stat.S_IMODE(current().config.stat().st_mode)
    assert mode == 0o600
    # 保存只落盘：磁盘状态 ok，但本进程仍用启动时的旧配置（重启才生效）
    assert manager.status == "ok"
    assert manager.needs_setup
    assert cfg.resolve_runtime("default").llm_call_options.model == "m1"
    # 落盘内容是提交的文档本身，不掺默认值/环境变量
    assert json.loads(current().config.read_text(encoding="utf-8")) == MINIMAL


def test_reload_after_save_sees_new_config():
    first = ConfigManager(current().config)
    first.load()
    first.save(MINIMAL)

    second = ConfigManager(current().config)
    cfg = second.load()
    assert not second.needs_setup
    assert cfg.models["m1"].context_window_tokens == 65536


def test_invalid_json_is_reported_not_raised():
    _write("{ not json")
    manager = ConfigManager(current().config)
    manager.load()

    assert manager.needs_setup
    assert manager.setup_payload()["reason"] == "invalid"
    assert manager.issues and "config.json" in manager.issues[0]["message"]


def test_structurally_invalid_config_reports_field_path():
    data = json.loads(json.dumps(MINIMAL))
    data["agentRuntimes"]["default"]["llmCallOptions"]["model"] = 123
    _write(data)

    manager = ConfigManager(current().config)
    manager.load()

    assert manager.setup_payload()["reason"] == "invalid"
    issue = manager.issues[0]
    assert "agentRuntimes.default.llmCallOptions.model" in issue["path"]


def test_config_without_model_binding_is_unbound():
    data = json.loads(json.dumps(MINIMAL))
    data["agentRuntimes"]["default"]["llmCallOptions"].pop("model")
    _write(data)

    manager = ConfigManager(current().config)
    manager.load()

    assert manager.needs_setup
    assert manager.setup_payload()["reason"] == "unbound"
    assert manager.issues[0]["path"].endswith("model")


def test_save_rejects_config_without_provider_endpoint():
    manager = ConfigManager(current().config)
    manager.load()
    broken = json.loads(json.dumps(MINIMAL))
    broken["providers"]["deepseek"]["apiBase"] = ""

    with pytest.raises(ConfigValidationError) as excinfo:
        manager.save(broken)

    assert any(i["path"] == "providers.deepseek.apiBase"
               for i in excinfo.value.issues)
    assert not current().config.exists()


def test_api_key_is_masked_and_restored_on_save():
    manager = ConfigManager(current().config)
    manager.load()
    manager.save(MINIMAL)

    doc = manager.masked_doc()
    assert doc["providers"]["deepseek"]["apiKey"] == MASKED_SECRET
    # 掩码只影响下发副本，磁盘与内存文档保持真实值
    assert manager.raw["providers"]["deepseek"]["apiKey"] == "sk-1"

    # 前端原样回传掩码：保存时回填磁盘上的真实密钥
    doc["models"]["m1"]["contextWindowTokens"] = 32768
    manager.save(doc)
    assert manager.raw["providers"]["deepseek"]["apiKey"] == "sk-1"

    # 重启后（新 manager 读盘）才拿到新窗口
    reloaded = ConfigManager(current().config)
    cfg = reloaded.load()
    assert cfg.models["m1"].context_window_tokens == 32768


def test_new_api_key_overrides_masked_value():
    manager = ConfigManager(current().config)
    manager.load()
    manager.save(MINIMAL)

    doc = manager.masked_doc()
    doc["providers"]["deepseek"]["apiKey"] = "sk-2"
    manager.save(doc)

    assert manager.raw["providers"]["deepseek"]["apiKey"] == "sk-2"


def test_feishu_app_secret_is_masked_and_restored():
    doc = json.loads(json.dumps(MINIMAL))
    doc["channels"] = {"feishu": {"appId": "cli-1", "appSecret": "secret-1"}}
    manager = ConfigManager(current().config)
    manager.load()
    manager.save(doc)

    masked = manager.masked_doc()
    assert masked["channels"]["feishu"]["appSecret"] == MASKED_SECRET

    manager.save(masked)
    assert manager.raw["channels"]["feishu"]["appSecret"] == "secret-1"


def test_feishu_enabled_switch_is_persisted_without_touching_credentials():
    doc = json.loads(json.dumps(MINIMAL))
    doc["channels"] = {"feishu": {"appId": "cli-1", "appSecret": "secret-1"}}
    manager = ConfigManager(current().config)
    manager.load()
    manager.save(doc)

    manager.set_feishu_enabled(True)

    saved = json.loads(current().config.read_text(encoding="utf-8"))
    assert saved["channels"]["feishu"]["enabled"] is True
    assert saved["channels"]["feishu"]["appSecret"] == "secret-1"
    assert manager.raw["channels"]["feishu"]["enabled"] is True
    assert manager.masked_doc()["channels"]["feishu"]["enabled"] is True


def test_feishu_qr_credentials_are_saved_with_enabled_flag():
    """扫码成功后只写凭据字段，并自动打开通道开关。"""
    manager = ConfigManager(current().config)
    manager.load()
    manager.save(json.loads(json.dumps(MINIMAL)))

    manager.set_feishu_credentials("cli-new", "secret-new")

    saved = json.loads(current().config.read_text(encoding="utf-8"))
    assert saved["channels"]["feishu"] == {
        "enabled": True, "appId": "cli-new", "appSecret": "secret-new"}
    assert stat.S_IMODE(current().config.stat().st_mode) == 0o600
    assert manager.masked_doc()["channels"]["feishu"]["appSecret"] == MASKED_SECRET


def test_reset_backs_up_broken_file_and_writes_skeleton():
    _write("{ not json")
    manager = ConfigManager(current().config)
    manager.load()

    backup = manager.reset()

    assert backup is not None and backup.exists()
    assert backup.read_text(encoding="utf-8") == "{ not json"
    assert json.loads(current().config.read_text(encoding="utf-8")) \
        == manager.masked_doc()
    # 重置只改磁盘；本进程仍需要重启才能用上骨架
    assert manager.status == "ok"
    assert ConfigManager(current().config).load().models


def test_custom_provider_is_accepted():
    """providers 是映射表：任意标识（如自建网关）都能保存并解析。"""
    doc = {
        "models": {"m1": {"provider": "my-proxy", "contextWindowTokens": 32000}},
        "providers": {
            "my-proxy": {"apiBase": "https://proxy.local/v1", "apiKey": "k"},
        },
        "agentRuntimes": {"default": {"llmCallOptions": {"model": "m1"}}},
    }
    manager = ConfigManager(current().config)
    manager.load()
    manager.save(doc)

    assert manager.raw["providers"]["my-proxy"]["apiBase"] == "https://proxy.local/v1"
    reloaded = ConfigManager(current().config)
    cfg = reloaded.load()
    assert reloaded.status == "ok"
    assert cfg.providers["my-proxy"].api_base == "https://proxy.local/v1"


def test_model_referencing_unknown_provider_is_rejected():
    broken = json.loads(json.dumps(MINIMAL))
    broken["models"]["m1"]["provider"] = "nope"

    manager = ConfigManager(current().config)
    manager.load()
    with pytest.raises(ConfigValidationError) as excinfo:
        manager.save(broken)

    assert any(i["path"] == "models.m1.provider"
               for i in excinfo.value.issues)


def test_provider_key_with_invalid_chars_is_rejected():
    broken = json.loads(json.dumps(MINIMAL))
    broken["providers"]["my proxy"] = {"apiBase": "https://x.local"}

    manager = ConfigManager(current().config)
    manager.load()
    with pytest.raises(ConfigValidationError) as excinfo:
        manager.save(broken)

    assert any(i["path"] == "providers" for i in excinfo.value.issues)


def test_maintenance_engine_starts_without_config(session_store):
    """首次使用（无配置）时维护任务跳过而不是拖垮启动。"""
    engine = MaintenanceEngine(Config(), session_store, current())
    asyncio.run(engine.run())
