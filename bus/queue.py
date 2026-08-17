"""
Message Queue Class
"""
import asyncio
import logging
from typing import TypeVar, Generic

from abc import ABC
from datacls import Message, MessageIn, MessageOut

T = TypeVar('T', bound=Message)

logger = logging.getLogger(__name__)


class MessageQueue(ABC, Generic[T]):

    def __init__(self):
        self.queue: asyncio.Queue[T] = asyncio.Queue()

    async def enqueue(self, message: T):
        await self.queue.put(message)

    async def dequeue(self) -> T:
        return await self.queue.get()

    @property
    def size(self) -> int:
        return self.queue.qsize()


class MessageInQueue(MessageQueue[MessageIn]):
    pass


class MessageOutQueue(MessageQueue[MessageOut]):
    pass


class MessageBus:
    """Inbound bus + channel-routed outbound bus.

    out_queue 是中央出站队列：由 run_out_dispatcher 按消息的 channel 字段
    路由到各 channel 通过 subscribe_out 认领的专属队列，保证每个 channel
    只消费自己的消息。
    """

    def __init__(self):
        self.in_queue = MessageInQueue()
        self.out_queue = MessageOutQueue()
        self._out_subscribers: dict[str, MessageOutQueue] = {}

    def subscribe_out(self, channel: str) -> MessageOutQueue:
        """登记并返回 channel 专属的出站队列（幂等）。"""
        if channel not in self._out_subscribers:
            self._out_subscribers[channel] = MessageOutQueue()
        return self._out_subscribers[channel]

    async def run_out_dispatcher(self) -> None:
        """后台任务：把中央出站队列的消息按 channel 分发给订阅者。"""
        while True:
            msg = await self.out_queue.dequeue()
            subscriber = self._out_subscribers.get(msg.channel)
            if subscriber is None:
                logger.warning("No channel subscribed for %r, message dropped", msg.channel)
                continue
            await subscriber.enqueue(msg)

