"""
Message Queue Class
"""
import asyncio
from typing import TypeVar, Generic

from abc import ABC
from datacls import Message, MessageIn, MessageOut

T = TypeVar('T', bound=Message)


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
    def __init__(self):
        self.in_queue = MessageInQueue()
        self.out_queue = MessageOutQueue()

