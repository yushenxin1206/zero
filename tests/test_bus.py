"""总线队列的容量契约：满了以后必须可预测，且失败对上游可见。"""
import asyncio
import contextlib

import pytest

from zero.bus.queue import BusQueueFull, MessageBus
from zero.models import MessageBody, MessageIn, StreamEvent


def _msg(text: str = "hi") -> MessageIn:
    return MessageIn(
        channel="cli", session_id="s", body=MessageBody(content=text))


def test_enqueue_nowait_rejects_when_full():
    """入站拒绝必须是显式异常，且不悄悄改动队列内容。"""
    bus = MessageBus(in_capacity=1)
    bus.in_queue.enqueue_nowait(_msg("first"))

    with pytest.raises(BusQueueFull) as exc:
        bus.in_queue.enqueue_nowait(_msg("second"))

    assert exc.value.name == "in"
    assert exc.value.capacity == 1
    assert bus.in_queue.size == 1


def test_enqueue_waits_for_space():
    """背压语义：队列满时等待，消费者取走一项后自动完成入队。"""

    async def scenario():
        bus = MessageBus(in_capacity=1)
        bus.in_queue.enqueue_nowait(_msg("first"))
        waiter = asyncio.create_task(
            bus.in_queue.enqueue(_msg("second")))
        await asyncio.sleep(0)
        assert not waiter.done(), "队列满时 enqueue 不应立即完成"

        assert (await bus.in_queue.dequeue()).body.content == "first"
        await asyncio.wait_for(waiter, 1)
        return (await bus.in_queue.dequeue()).body.content

    assert asyncio.run(scenario()) == "second"


async def _until(predicate, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("等待条件超时")
        await asyncio.sleep(0)


def test_dispatcher_evicts_oldest_when_channel_backs_up():
    """慢 channel 不能反压核心：订阅队列满时丢最旧，保留最新事件。"""

    async def scenario():
        bus = MessageBus(stream_capacity=2)
        subscriber = bus.subscribe_stream("web")  # 故意不消费
        dispatcher = asyncio.create_task(bus.run_stream_dispatcher())
        try:
            for i in range(3):
                await bus.stream_queue.enqueue(
                    StreamEvent(kind="delta", content=str(i), channel="web"))
            await _until(lambda: bus.stream_queue.size == 0
                         and subscriber.size == 2)
        finally:
            dispatcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await dispatcher
        return [
            (await subscriber.dequeue()).content for _ in range(2)
        ]

    assert asyncio.run(scenario()) == ["1", "2"]
