"""Offline smoke test: background turns (scheduled tasks, interrupted
turns) are demultiplexed by turn_id and rendered asynchronously on the
CLI channel — no real LLM, no TTY.

Covers:
  1. A scheduled task's stream events + final MessageOut render exactly
     once as a "定时任务" panel, bump the unread counter, and fire a toast.
  2. An interactive turn still renders through _render_turn and does NOT
     leak into the background pipeline; a late duplicate MessageOut is
     discarded.
  3. A finished-but-unrendered turn (e.g. Ctrl+C interrupted) renders as
     a "后台回答" panel when its late events arrive.
  4. /tasks lists persisted scheduled tasks.

Run: python tests/smoke_cli_async_render.py
"""
import asyncio
import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import session.store as store_mod
import scheduler.store as sched_mod

# Isolate storage: redirect sessions/scheduler roots to a temp dir.
_tmp = tempfile.TemporaryDirectory()
store_mod.SESSIONS_ROOT = Path(_tmp.name) / "sessions"
sched_mod.SCHEDULER_ROOT = Path(_tmp.name) / "scheduler"
sched_mod.TASKS_FILE = sched_mod.SCHEDULER_ROOT / "tasks.jsonl"

from rich.console import Console

from bus.queue import MessageBus
from datacls import MessageBody, MessageOut, ScheduleTask, StreamEvent
from scheduler.store import SchedulerStore
import channels.cli as cli_mod
from channels.cli import CLIChannel, CHANNEL


async def main():
    bus = MessageBus()
    ch = CLIChannel(bus)
    buf = io.StringIO()
    ch.console = Console(file=buf, highlight=False, width=100)

    toasts = []
    cli_mod.toast = lambda title, msg: toasts.append((title, msg))

    tasks = [
        asyncio.create_task(bus.run_out_dispatcher()),
        asyncio.create_task(bus.run_stream_dispatcher()),
        asyncio.create_task(ch._stream_listener()),
        asyncio.create_task(ch._out_listener()),
        asyncio.create_task(ch._async_render_worker()),
    ]
    try:
        # --- 1. scheduled task result renders asynchronously, exactly once ---
        task = ScheduleTask(
            channel=CHANNEL, session_id="sess-sched",
            instruction="[Scheduled Task]每天总结科技新闻",
            schedule_type="once",
        )
        SchedulerStore.create(task)
        meta = {"source": "scheduler", "task_id": task.id}
        for kind, content in [("delta", "今日"), ("delta", "要闻"),
                              ("done", "今日要闻汇总")]:
            await bus.stream_queue.enqueue(StreamEvent(
                channel=CHANNEL, session_id="sess-sched", turn_id="turn-sched",
                kind=kind, content=content, metadata=meta))
        await bus.out_queue.enqueue(MessageOut(
            channel=CHANNEL, session_id="sess-sched",
            body=MessageBody(content="今日要闻汇总"),
            metadata={**meta, "turn_id": "turn-sched"}))
        await asyncio.sleep(0.5)
        out = buf.getvalue()
        assert "今日要闻汇总" in out, out
        assert "每天总结科技新闻" in out, out  # subtitle carries the instruction
        assert out.count("zero · 定时任务") == 1, out  # dedup done/MessageOut
        assert ch._unread == 1, ch._unread
        assert toasts and toasts[0][0] == "zero · 定时任务完成", toasts
        print("scheduled async render ok (panel + unread + toast, no dup)")

        # --- 2. interactive turn renders via _render_turn, no bg leak ---
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
            metadata={"turn_id": "turn-user"}))
        await asyncio.wait_for(render, timeout=3)
        out = buf.getvalue()
        assert "用户回复" in out, out
        assert "后台回答" not in out, out

        # late duplicate MessageOut after the turn ended → discarded
        await bus.out_queue.enqueue(MessageOut(
            channel=CHANNEL, session_id="sess-user",
            body=MessageBody(content="用户回复"),
            metadata={"turn_id": "turn-user"}))
        await asyncio.sleep(0.3)
        assert "后台回答" not in buf.getvalue(), buf.getvalue()
        print("interactive turn ok (no bg leak, late MessageOut discarded)")

        # --- 3. interrupted turn's late completion renders as 后台回答 ---
        await bus.stream_queue.enqueue(StreamEvent(
            channel=CHANNEL, session_id="sess-user", turn_id="turn-old",
            kind="done", content="迟到的回答"))
        await asyncio.sleep(0.3)
        out = buf.getvalue()
        assert "后台回答" in out, out
        assert "迟到的回答" in out, out
        print("interrupted-turn fallback render ok")

        # --- 4. /tasks lists persisted tasks ---
        ch.session_id = "sess-sched"
        buf.truncate(0)
        buf.seek(0)
        ch._handle_tasks("all")
        out = buf.getvalue()
        assert task.id[:8] in out, out
        assert "每天总结科技新闻" in out, out
        print("/tasks listing ok")

        print("ALL ASYNC RENDER SMOKE TESTS PASSED")
    finally:
        for t in tasks:
            t.cancel()


asyncio.run(main())
