"""
Background-turn pipeline for the CLI channel.

Output that does not belong to the interactive turn being rendered — results
of scheduled tasks, turns the user interrupted, subagent reports — is
accumulated here and surfaced asynchronously above the prompt. Both the
stream ``done`` event and the final ``MessageOut`` arrive for a turn; the
first one wins and the second is discarded, so each result renders once.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Literal

from rich.text import Text

from zero.channels.cli.input import CliInput
from zero.channels.cli.renderer import CliRenderer
from zero.models import MessageOut, Origin, StreamEvent
from zero.utils.notify import toast

BackgroundKind = Literal["turn", "approval_blocked", "subagent_result"]


@dataclass
class BackgroundRecord:
    """一条待异步渲染的后台结果。

    origin 同时承载「谁触发的」（user/scheduler/subagent）与对应 id，
    取代此前 source/task_id 两个平行字符串——二者本就来自同一个 Origin。
    """
    turn_id: str
    session_id: str
    text: str
    origin: Origin = field(default_factory=Origin)
    error: bool = False
    streamed: bool = False
    kind: BackgroundKind = "turn"


class CliBackground:
    """Demultiplexes background turns and renders their results."""

    def __init__(self, renderer: CliRenderer, input_: CliInput,
                 render_lock: asyncio.Lock, *, stream_enabled):
        self._renderer = renderer
        self._input = input_
        self._render_lock = render_lock
        self._stream_enabled = stream_enabled  # callable -> bool
        # background turns being accumulated
        self._turns: dict[str, dict] = {}
        # turn_ids already finalized (dedup stream-done vs MessageOut)
        self._finalized: set[str] = set()
        # completed results waiting to be rendered (FIFO worker)
        self._results: asyncio.Queue[BackgroundRecord] = asyncio.Queue()

    def mark_finalized(self, turn_id: str) -> None:
        """Remember a turn as done so late duplicates are dropped."""
        self._finalized.add(turn_id)

    def deliver(self, message: MessageOut) -> None:
        """Consume one final outbound message that is not the active turn."""
        self._finalize(
            message.turn_id or message.id,
            text=message.body.content or "",
            session_id=message.session_id,
            origin=message.origin,
        )

    def handle_event(self, event: StreamEvent) -> None:
        """Consume one stream event that is not the active turn."""
        if event.kind == "subagent_result":
            # 子 agent 结果原文：不走 turn 累积，直接进异步渲染管线独立展示
            self._notify_subagent_result(event)
            return
        acc = self._turns.setdefault(event.turn_id, {
            "text": "",
            "session_id": event.session_id,
            "origin": event.origin,
            "buf": "",
            "last_flush": 0.0,
            "streamed": False,
        })
        if event.kind == "delta":
            acc["text"] += event.content
            if self._stream_enabled():
                self._push(acc, event.content)
        elif event.kind == "reasoning":
            # 后台 turn（定时任务、已转后台的轮次）的思考过程默认忽略，
            # 避免噪音；正文仍照常累积与渲染
            return
        elif event.kind == "tool_call":
            if self._stream_enabled():
                self._push(acc, f"\n[调用工具 {event.content}]\n")
        elif event.kind == "approval":
            # turn 已被 Ctrl+C 移入后台：审批请求无法就地提问，后端将
            # 超时 fail-closed。借异步结果管线（自带渲染锁串行）给用户
            # 一条明确提示，而不是静默丢弃
            self._notify_approval_blocked(event)
        elif event.kind in ("done", "error"):
            self._finalize(
                event.turn_id,
                text=event.content or acc["text"],
                session_id=acc["session_id"],
                origin=acc["origin"],
                error=event.kind == "error",
                streamed=acc["streamed"],
            )

    async def run(self) -> None:
        """Render completed background results one at a time.

        Serialized with interactive Live rendering through the render lock,
        so a panel never tears into an active streaming frame.
        """
        while True:
            rec = await self._results.get()
            async with self._render_lock:
                self._renderer.render_async_record(
                    rec, prompt_active=self._input.active)
            self._input.mark_unread()
            if rec.origin.kind == "scheduler":
                toast("zero · 定时任务完成", rec.text)
            elif rec.kind == "approval_blocked":
                toast("zero · 审批未受理", rec.text)
            elif rec.kind == "subagent_result":
                toast("zero · 子任务完成", rec.text)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _notify_approval_blocked(self, event: StreamEvent) -> None:
        """后台 turn 的审批请求无法就地回答，提示用户该命令将按超时拒绝。"""
        req = event.approval_request()
        text = ("有 shell 命令需要人工审批，但当前 turn 已转入后台，无法就地"
                "回答，将按超时自动拒绝。")
        if req is not None and req.summary:
            text += f"\n\n命令：`{req.summary}`"
        self._results.put_nowait(BackgroundRecord(
            turn_id=event.turn_id,
            session_id=event.session_id,
            text=text,
            origin=event.origin,
            kind="approval_blocked",
        ))

    def _notify_subagent_result(self, event: StreamEvent) -> None:
        """子 agent 结果原文：直接送入异步结果管线，独立面板原样展示。"""
        origin = event.origin
        if not origin.task_id:
            origin = Origin(kind="subagent", task_id=event.turn_id)
        self._results.put_nowait(BackgroundRecord(
            turn_id=event.turn_id,
            session_id=event.session_id,
            text=event.content,
            origin=origin,
            kind="subagent_result",
        ))

    def _push(self, acc: dict, chunk: str) -> None:
        """Live-print background output above the prompt (/bgstream on).

        Throttled and skipped while an interactive Live render holds the
        render lock — the completion panel will carry the full text then.
        """
        if self._render_lock.locked():
            return
        acc["buf"] += chunk
        now = time.monotonic()
        if (now - acc["last_flush"] < 0.4
                and "\n" not in acc["buf"] and len(acc["buf"]) < 200):
            return
        acc["last_flush"] = now
        text, acc["buf"] = acc["buf"], ""
        if not acc["streamed"]:
            self._renderer.print_above_prompt(
                "[dim]⏰ 后台任务输出：[/dim]", prompt_active=self._input.active)
            acc["streamed"] = True
        self._renderer.print_above_prompt(
            Text(text, style="dim"), prompt_active=self._input.active, end="")

    def _finalize(self, turn_id: str, *, text: str, session_id: str,
                  origin: Origin, error: bool = False,
                  streamed: bool = False) -> None:
        """Finalize a background turn exactly once."""
        if not turn_id:
            return
        if len(self._finalized) > 1000:
            self._finalized.clear()
        if turn_id in self._finalized:
            return
        self._finalized.add(turn_id)
        self._turns.pop(turn_id, None)
        text = (text or "").strip()
        if not text:
            return
        self._results.put_nowait(BackgroundRecord(
            turn_id=turn_id,
            session_id=session_id,
            text=text,
            origin=origin,
            error=error,
            streamed=streamed,
        ))
