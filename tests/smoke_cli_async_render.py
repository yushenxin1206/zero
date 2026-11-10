"""Offline smoke tests: background turns (scheduled tasks, interrupted
turns) are demultiplexed by turn_id and rendered asynchronously on the
CLI channel — no real LLM, no TTY.

Covers:
  1. A scheduled task's stream events + final MessageOut render exactly
     once as a "定时任务" panel, bump the unread counter, and fire a toast.
  2. An interactive turn renders through _render_turn and does NOT leak
     into the background pipeline; a late duplicate MessageOut is discarded.
  3. A finished-but-unrendered turn (e.g. Ctrl+C interrupted) renders as
     a "后台回答" panel when its late events arrive.
  4. /tasks lists persisted scheduled tasks.
"""
import asyncio
import io

from rich.console import Console

import zero.channels.cli.background as background_mod
from zero.bus.queue import MessageBus
from zero.channels.cli import CHANNEL, CLIChannel
from zero.models import MessageBody, MessageOut, Origin, StreamEvent
from zero.scheduler.models import ScheduleTask
from tests.support import make_data_paths, make_permission, make_scheduler_store, make_store


async def run_with_channel(scenario):
    """Start a CLI channel + bus dispatchers, then run one scenario."""
    bus = MessageBus()
    buf = io.StringIO()
    ch = CLIChannel(bus, session_store=make_store(), data_paths=make_data_paths(), permission=make_permission(),
                    scheduler_store=make_scheduler_store(), console=Console(
        file=buf, highlight=False, width=100))
    dispatchers = [
        asyncio.create_task(bus.run_out_dispatcher()),
        asyncio.create_task(bus.run_stream_dispatcher()),
    ]
    ch.start_consumers()
    ch.spawn(ch.background.run())
    try:
        return await scenario(bus, ch, buf)
    finally:
        await ch.stop_consumers()
        for task in dispatchers:
            task.cancel()


def test_scheduled_task_result_renders_once(monkeypatch):
    toasts = []
    monkeypatch.setattr(
        background_mod, "toast", lambda title, msg: toasts.append((title, msg)))

    async def scenario(bus, ch, buf):
        task = ScheduleTask(
            channel=CHANNEL, session_id="sess-sched",
            instruction="[Scheduled Task]每天总结科技新闻",
            schedule_type="once",
        )
        make_scheduler_store().create(task)
        origin = Origin(kind="scheduler", task_id=task.id)
        for kind, content in [("delta", "今日"), ("delta", "要闻"),
                              ("done", "今日要闻汇总")]:
            await bus.stream_queue.enqueue(StreamEvent(
                channel=CHANNEL, session_id="sess-sched", turn_id="turn-sched",
                kind=kind, content=content, origin=origin))
        await bus.out_queue.enqueue(MessageOut(
            channel=CHANNEL, session_id="sess-sched",
            body=MessageBody(content="今日要闻汇总"),
            origin=origin, turn_id="turn-sched"))
        await asyncio.sleep(0.5)

        out = buf.getvalue()
        assert "今日要闻汇总" in out, out
        assert "每天总结科技新闻" in out, out  # subtitle carries the instruction
        assert out.count("zero · 定时任务") == 1, out  # dedup done/MessageOut
        assert ch.input.unread == 1, ch.input.unread
        assert toasts and toasts[0][0] == "zero · 定时任务完成", toasts

    asyncio.run(run_with_channel(scenario))


def test_interactive_turn_does_not_leak_to_background():
    async def scenario(bus, ch, buf):
        ch._active_turn_id = "turn-user"
        render = asyncio.create_task(ch._render_turn("turn-user"))
        await bus.stream_queue.enqueue(StreamEvent(
            channel=CHANNEL, session_id="sess-user", turn_id="turn-user",
            kind="delta", content="用户回复"))
        await bus.stream_queue.enqueue(StreamEvent(
            channel=CHANNEL, session_id="sess-user", turn_id="turn-user",
            kind="done", content="用户回复"))
        await bus.out_queue.enqueue(MessageOut(
            channel=CHANNEL, session_id="sess-user",
            body=MessageBody(content="用户回复"),
            turn_id="turn-user"))
        await asyncio.wait_for(render, timeout=3)
        out = buf.getvalue()
        assert "用户回复" in out, out
        assert "后台回答" not in out, out

        # late duplicate MessageOut after the turn ended → discarded
        await bus.out_queue.enqueue(MessageOut(
            channel=CHANNEL, session_id="sess-user",
            body=MessageBody(content="用户回复"),
            turn_id="turn-user"))
        await asyncio.sleep(0.3)
        assert "后台回答" not in buf.getvalue(), buf.getvalue()

    asyncio.run(run_with_channel(scenario))


def test_interrupted_turn_renders_as_background():
    async def scenario(bus, ch, buf):
        await bus.stream_queue.enqueue(StreamEvent(
            channel=CHANNEL, session_id="sess-user", turn_id="turn-old",
            kind="done", content="迟到的回答"))
        await asyncio.sleep(0.3)
        out = buf.getvalue()
        assert "后台回答" in out, out
        assert "迟到的回答" in out, out

    asyncio.run(run_with_channel(scenario))


def test_tasks_listing():
    task = ScheduleTask(
        channel=CHANNEL, session_id="sess-sched",
        instruction="[Scheduled Task]每天总结科技新闻",
        schedule_type="once",
    )
    make_scheduler_store().create(task)

    async def scenario(bus, ch, buf):
        ch.service.session_id = "sess-sched"
        await ch.commands.dispatch("/tasks all")
        out = buf.getvalue()
        assert task.id[:8] in out, out
        assert "每天总结科技新闻" in out, out

    asyncio.run(run_with_channel(scenario))
