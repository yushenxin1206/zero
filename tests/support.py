"""测试用最小配置：不依赖磁盘上的 config.json。"""
import asyncio
import json
import time

from zero.config import Config
from zero.paths import current
from zero.scheduler.store import SchedulerStore
from zero.session.store import SessionStore


def make_store() -> SessionStore:
    """测试边界：按当前隔离根构造会话存储，与组合根的构造方式一致。"""
    return SessionStore(current().sessions)


def make_scheduler_store() -> SchedulerStore:
    """测试边界：按当前隔离根构造调度任务存储。"""
    return SchedulerStore(current().tasks)


def make_test_config() -> Config:
    return Config.model_validate({
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


async def wait_turn_closed(
        channel: str, session_id: str, turn_id: str,
        timeout: float = 10.0) -> dict:
    """等到本轮 turn_end 真正落盘，返回该记录。

    load_history 会把没有 turn_end 的轮次当成进程崩溃残留、整轮排除，
    所以「收到 MessageOut」不等于「历史可读」：MessageOut 先于 end_turn
    入队。这里等磁盘上的事实，而不是等固定时长。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        path = make_store()._session_file(channel, session_id)
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue  # 正在写入的半行，下一轮再读
                if record.get("kind") == "turn_end" \
                        and record.get("turn_id") == turn_id:
                    return record
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"turn {turn_id} 未在 {timeout}s 内闭合 "
        f"(session={channel}:{session_id})")
