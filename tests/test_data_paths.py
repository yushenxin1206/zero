"""P0-2 验收：导入零副作用 + 同进程多数据根隔离。"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from zero.config import load_config
from zero.data_paths import DataPaths
from zero.session.store import SessionStore
from zero.utils.file import save_or_create_file


def test_import_zero_has_no_data_root_io():
    """import zero / zero.main 不得读或写 ~/.zero。"""
    code = """
import builtins
import pathlib

calls = []
_open = builtins.open
builtins.open = lambda f, *a, **k: (
    calls.append(str(f)), _open(f, *a, **k))[1]
_exists = pathlib.Path.exists
pathlib.Path.exists = lambda self: (calls.append(str(self)), _exists(self))[1]
_mkdir = pathlib.Path.mkdir
pathlib.Path.mkdir = lambda self, *a, **k: (
    calls.append(str(self)), _mkdir(self, *a, **k))[1]
_write = pathlib.Path.write_text
pathlib.Path.write_text = lambda self, *a, **k: (
    calls.append(str(self)), _write(self, *a, **k))[1]

import zero
import zero.main

bad = [c for c in calls if ".zero" in c]
assert not bad, bad
assert not hasattr(zero.config, "default_config")
"""
    repo = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo,
        env={**os.environ, "PYTHONPATH": str(repo)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "Do not" not in result.stderr


def test_two_data_roots_do_not_interfere(tmp_path):
    """同一进程内两个数据根：会话、配置、记忆各写各的。

    不依赖进程级 current()：每个任务持有自己的 DataPaths 并显式注入。
    """
    root_a = DataPaths(tmp_path / "a")
    root_b = DataPaths(tmp_path / "b")

    async def worker(data_paths: DataPaths, marker: str) -> dict:
        store = SessionStore(data_paths.sessions)
        sid = store.create_session("cli", f"src-{marker}")
        save_or_create_file(data_paths.config, json.dumps({
            "models": {marker: {
                "provider": "deepseek",
                "contextWindowTokens": 65536,
            }},
            "providers": {"deepseek": {
                "apiBase": "https://test.local", "apiKey": "test"}},
        }))
        data_paths.memory.parent.mkdir(parents=True, exist_ok=True)
        data_paths.memory.write_text(marker, encoding="utf-8")
        await asyncio.sleep(0)  # 让两个任务真正交错
        return {
            "root": data_paths.root,
            "sid": sid,
            "models": set(load_config(data_paths.config).models),
            "memory": data_paths.memory.read_text(encoding="utf-8"),
            "session_dir": store.session_dir("cli", sid),
        }

    async def run() -> list[dict]:
        return await asyncio.gather(
            asyncio.create_task(worker(root_a, "a")),
            asyncio.create_task(worker(root_b, "b")),
        )

    a, b = asyncio.run(run())

    assert a["root"] == root_a.root
    assert b["root"] == root_b.root
    assert a["sid"] != b["sid"]
    assert a["models"] == {"a"}
    assert b["models"] == {"b"}
    assert a["memory"] == "a"
    assert b["memory"] == "b"
    assert a["session_dir"].is_relative_to(root_a.root)
    assert b["session_dir"].is_relative_to(root_b.root)
    assert (root_a.sessions / "cli" / "index.json").exists()
    assert (root_b.sessions / "cli" / "index.json").exists()
