"""Shared pytest setup.

Two jobs:

1. **Decouple from the developer's real ``~/.zero``.** ``zero.config``
   evaluates ``ZERO_ROOT`` and ``default_config`` at import time (a known
   P0-2 issue). We rewrite those module attributes before importing any
   other ``zero`` module, so every test sees a throwaway data root and a
   minimal in-memory config instead of the user's real setup.

2. **Per-test filesystem isolation.** Storage modules read their root path
   lazily, so each test gets its own sessions / scheduler / seatbelt / memory
   roots. Tests can run in one process without leaking state into each other.

The process HOME is deliberately left untouched: the security tests classify
paths relative to the user's home (e.g. "outside the envelope but in HOME"),
and pointing HOME into TMPDIR would make those paths fall into the writable
TMPDIR root and invalidate the assertions.
"""
import atexit
import shutil
import tempfile
from pathlib import Path

import pytest

import zero.config as config_mod

# --- (1) replace config roots before the rest of zero is imported ----------
_TEST_ROOT = Path(tempfile.mkdtemp(prefix="zero-test-root-"))
atexit.register(shutil.rmtree, _TEST_ROOT, True)
_TEST_ZERO = _TEST_ROOT / ".zero"
_TEST_ZERO.mkdir(parents=True, exist_ok=True)

config_mod.ZERO_ROOT = _TEST_ZERO
config_mod.CONFIG_PATH = _TEST_ZERO / "config.json"
config_mod.MEMORY_PATH = _TEST_ZERO / "memory.md"
config_mod.default_config = config_mod.Config.model_validate({
    "models": {
        "test-model": {
            "provider": "deepseek",
            "contextWindowTokens": 65536,
        },
    },
    "agentRuntimes": {"default": {"llmCallOptions": {"model": "test-model"}}},
    "providers": {
        "deepseek": {"apiBase": "https://test.local", "apiKey": "test"},
    },
})

import zero.core.context as context_mod  # noqa: E402
import zero.permission.seatbelt as seatbelt  # noqa: E402
import zero.scheduler.store as scheduler_store  # noqa: E402
import zero.session.store as session_store  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """每例测试独立的数据根目录（惰性读取，patch 模块常量即可生效）。"""
    monkeypatch.setattr(session_store, "SESSIONS_ROOT", tmp_path / "sessions")
    monkeypatch.setattr(
        scheduler_store, "SCHEDULER_ROOT", tmp_path / "scheduler")
    monkeypatch.setattr(
        scheduler_store, "TASKS_FILE",
        tmp_path / "scheduler" / "tasks.jsonl")
    # 沙箱 profile 临时文件不落进真实 ~/.zero/run
    monkeypatch.setattr(seatbelt, "PROFILES_DIR", tmp_path / "run")
    # 记忆文件同样隔离，避免读到开发者真实记忆
    monkeypatch.setattr(context_mod, "MEMORY_PATH", tmp_path / "memory.md")
    monkeypatch.setattr(
        context_mod, "current_project_memory_path",
        lambda: tmp_path / "project_memory.md")
    return tmp_path
