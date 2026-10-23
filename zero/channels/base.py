"""
Channel contract shared by every transport.

A channel is a transport adapter: it carries user input into the bus and
turns bus output into whatever the user can perceive (terminal frames,
WebSocket frames, Feishu messages). Business rules live in
``ChannelService``; rendering lives in the channel itself.

``Channel`` defines the four things the rest of the system may assume about
a channel. ``BusChannel`` implements the boilerplate every bus-bound
transport needs: claim its routed queues and run the two standing consumers
that forward queue traffic into ``deliver`` / ``deliver_stream``.
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import ClassVar, Protocol, runtime_checkable

from zero.bus.queue import MessageBus
from zero.models import MessageOut, StreamEvent


@runtime_checkable
class Channel(Protocol):
    """Minimal contract every transport implements."""

    name: ClassVar[str]

    async def start(self) -> None:
        """Run the channel until it exits or ``stop()`` is called."""

    async def stop(self) -> None:
        """Ask the channel to wind down."""

    async def deliver(self, message: MessageOut) -> None:
        """Handle one final agent message routed to this channel."""

    async def deliver_stream(self, event: StreamEvent) -> None:
        """Handle one incremental stream event routed to this channel."""


class BusChannel(ABC):
    """Base class for channels wired to the in-process MessageBus.

    Owns the channel's two routed queues and the consumer tasks that feed
    them into the subclass hooks. Subclasses only add their transport
    lifecycle (socket server, terminal loop, …); shutdown of every task
    started here is centralized in ``stop_consumers``.
    """

    name: ClassVar[str] = ""

    def __init__(self, msg_bus: MessageBus):
        self.msg_bus = msg_bus
        self._out_queue = msg_bus.subscribe_out(self.name)
        self._stream_queue = msg_bus.subscribe_stream(self.name)
        self._workers: list[asyncio.Task] = []

    def start_consumers(self) -> None:
        """Start the standing out/stream consumers."""
        self.spawn(self._consume_out())
        self.spawn(self._consume_stream())

    def spawn(self, coro) -> asyncio.Task:
        """Run a long-lived coroutine under this channel's lifecycle."""
        task = asyncio.create_task(coro)
        self._workers.append(task)
        return task

    async def stop_consumers(self) -> None:
        """Cancel and await every task started by this channel."""
        for task in self._workers:
            task.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def _consume_out(self) -> None:
        while True:
            await self.deliver(await self._out_queue.dequeue())

    async def _consume_stream(self) -> None:
        while True:
            await self.deliver_stream(await self._stream_queue.dequeue())

    @abstractmethod
    async def deliver(self, message: MessageOut) -> None:
        """Handle one final agent message routed to this channel."""

    @abstractmethod
    async def deliver_stream(self, event: StreamEvent) -> None:
        """Handle one incremental stream event routed to this channel."""
