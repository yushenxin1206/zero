"""轮次摘要：一轮一行，覆盖 completed / cancelled / error 三个出口。

用 stub runner 驱动真实 AgentLoop（真实 SessionStore/Context，写入
tmp 数据根），用 caplog 断言摘要字段与"只记一次"。
"""
import asyncio
import contextlib
import logging
import re

from types import SimpleNamespace

import pytest

from zero.bus.queue import MessageBus
from zero.core import loop as loop_mod
from zero.core.cancellation import TurnCancelled
from zero.core.hooks import Hooks
from zero.core.loop import AgentLoop
from zero.models import MessageBody, MessageIn, StreamEvent, TokenUsage
from tests.support import make_data_paths, make_permission, make_store, make_test_config

SESSION = "s-summary"

SUMMARY_RE = re.compile(
    r"^turn status=(?P<status>\S+) turn_id=(?P<turn_id>\S+)"
    r" channel=(?P<channel>\S+) session=(?P<session>\S+)"
    r" model=(?P<model>\S+) ms=(?P<ms>\d+) tools=(?P<tools>\d+)"
    r" tokens=(?P<tokens>\S+)(?: error=(?P<error>.*))?$")


def install_runner(monkeypatch, events, fail=None):
    """把 AgentRunner 换成固定产出 events 的桩，再按需抛异常。"""

    class StubRunner:
        def __init__(self, **_kwargs):
            self.runtime_config = SimpleNamespace(
                llm_call_options=SimpleNamespace(model="stub-model"))

        async def run_stream(self):
            for event in events:
                yield event
            if fail is not None:
                raise fail

    monkeypatch.setattr(loop_mod, "AgentRunner", StubRunner)


class SummarySignal(logging.Handler):
    """把「摘要行已发出」变成进程内事件，测试据此确定性等待，不轮询。"""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.arrived = asyncio.Event()

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage().startswith("turn status="):
            self.arrived.set()


async def run_turn() -> MessageIn:
    bus = MessageBus()
    loop = AgentLoop(
        msg_bus=bus, config=make_test_config(), hooks=Hooks(),
        session_store=make_store(), data_paths=make_data_paths(), permission=make_permission())
    signal = SummarySignal()
    logger = logging.getLogger("zero.core.loop")
    logger.addHandler(signal)
    try:
        loop_task = asyncio.create_task(loop.run())
        msg = MessageIn(
            channel="cli", session_id=SESSION, body=MessageBody(content="hi"))
        await bus.in_queue.enqueue(msg)
        await asyncio.wait_for(bus.out_queue.dequeue(), 5)
        # 等摘要真正落定再关 loop：否则取消可能落在 end_turn 之前，把状态
        # 从 completed/error 改写成 cancelled（测试竞态，不是产品行为）
        await asyncio.wait_for(signal.arrived.wait(), 5)
        loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop_task
    finally:
        logger.removeHandler(signal)
    return msg


def summary_records(caplog) -> list[str]:
    return [
        record.getMessage() for record in caplog.records
        if record.name == "zero.core.loop"
        and record.getMessage().startswith("turn status=")
    ]


def summaries(caplog) -> dict[str, str]:
    """取出本轮摘要行并解析（同时断言一轮只有一条）。"""
    lines = summary_records(caplog)
    assert len(lines) == 1, lines
    match = SUMMARY_RE.match(lines[0])
    assert match, lines[0]
    return {key: value for key, value in match.groupdict().items()
            if value is not None}


def test_completed_turn_summary(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="zero.core.loop")
    install_runner(monkeypatch, [
        StreamEvent(kind="tool_call", content="read(path='a')"),
        StreamEvent(kind="delta", content="hello"),
        StreamEvent(
            kind="done", content="hello",
            token_usage=TokenUsage(
                prompt_tokens=10, completion_tokens=5, total_tokens=15)),
    ])
    msg = asyncio.run(run_turn())

    stat = summaries(caplog)
    assert stat["status"] == "completed"
    assert stat["turn_id"] == msg.id
    assert stat["channel"] == "cli"
    assert stat["session"] == f"cli:{SESSION}"
    assert stat["model"] == "stub-model"
    assert stat["tools"] == "1"
    assert stat["tokens"] == "10/5/15"
    assert int(stat["ms"]) >= 0
    assert "error" not in stat


def test_cancelled_turn_summary(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="zero.core.loop")
    install_runner(
        monkeypatch,
        [StreamEvent(kind="delta", content="partial")],
        fail=TurnCancelled())
    asyncio.run(run_turn())

    stat = summaries(caplog)
    assert stat["status"] == "cancelled"
    assert stat["tokens"] == "-"      # 取消轮拿不到 usage
    assert "error" not in stat


def test_error_turn_summary_keeps_reason_on_one_line(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="zero.core.loop")
    install_runner(monkeypatch, [
        StreamEvent(kind="error", content="HTTPError: 401 Unauthorized\nretry"),
    ])
    asyncio.run(run_turn())

    stat = summaries(caplog)
    assert stat["status"] == "error"
    assert stat["error"] == "HTTPError: 401 Unauthorized retry"
    assert "\n" not in stat["error"]


@pytest.mark.parametrize("missing", ["channel", "session"])
def test_summary_fields_always_present(monkeypatch, caplog, missing):
    """三个状态共用同一格式：字段缺失会在这里暴露。"""
    caplog.set_level(logging.INFO, logger="zero.core.loop")
    install_runner(monkeypatch, [StreamEvent(kind="done", content="ok")])
    asyncio.run(run_turn())
    assert missing in summaries(caplog)
