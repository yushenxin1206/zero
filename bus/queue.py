"""
Message Queue Class
"""
import asyncio

from bus.message import Message


class MessageQueue:
    def __init__(self):
        self.queue: asyncio.Queue[Message] = asyncio.Queue()

    async def enqueue(self, message):
        await self.queue.put(message)

    async def dequeue(self):
        return await self.queue.get()

    @property
    def size(self):
        return self.queue.qsize()


class MessageInQueue(MessageQueue):
    def __init__(self):
        super().__init__()


class MessageOutQueue(MessageQueue):
    def __init__(self):
        super().__init__()


class MessageBus:
    def __init__(self):
        self.in_queue = MessageInQueue()
        self.out_queue = MessageOutQueue()
