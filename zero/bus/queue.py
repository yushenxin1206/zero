"""
Message Queue Class
"""
import asyncio
import logging
from typing import TypeVar, Generic

from abc import ABC
from zero.models import Base, MessageIn, MessageOut, StreamEvent

T = TypeVar('T', bound=Base)

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


class StreamEventQueue(MessageQueue[StreamEvent]):
    pass


class ApprovalBroker:
    """进程内审批答案回填器。

    审批发起方（任意插件/组件）在发出 approval 事件前 register 一个
    Future；channel 端作答后调用 resolve 回填。channel 只面向本接口
    编程，不认识审批的业务来源，来源方也不 import channel。
    """

    def __init__(self):
        self._pending: dict[str, asyncio.Future] = {}

    def register(self, req_id: str, fut: asyncio.Future) -> None:
        self._pending[req_id] = fut

    def resolve(self, req_id: str, answer: str) -> bool:
        """channel 侧回填答案。返回 False 表示请求不存在或已完成。"""
        fut = self._pending.get(req_id)
        if fut is None or fut.done():
            return False
        fut.set_result(answer)
        return True

    def discard(self, req_id: str) -> None:
        self._pending.pop(req_id, None)


class MessageBus:
    """Inbound bus + channel-routed outbound bus.

    out_queue / stream_queue 是中央出站队列：由 dispatcher 按消息的 channel
    字段路由到各 channel 通过 subscribe_* 认领的专属队列，保证每个 channel
    只消费自己的消息。
    """

    def __init__(self):
        self.in_queue = MessageInQueue()
        self.out_queue = MessageOutQueue()
        self.stream_queue = StreamEventQueue()
        self.approvals = ApprovalBroker()
        self._out_subscribers: dict[str, MessageOutQueue] = {}
        self._stream_subscribers: dict[str, StreamEventQueue] = {}

    def subscribe_out(self, channel: str) -> MessageOutQueue:
        """登记并返回 channel 专属的出站队列（幂等）。"""
        if channel not in self._out_subscribers:
            self._out_subscribers[channel] = MessageOutQueue()
        return self._out_subscribers[channel]

    def subscribe_stream(self, channel: str) -> StreamEventQueue:
        """登记并返回 channel 专属的流式事件队列（幂等）。"""
        if channel not in self._stream_subscribers:
            self._stream_subscribers[channel] = StreamEventQueue()
        return self._stream_subscribers[channel]

    async def run_out_dispatcher(self) -> None:
        """后台任务：把中央出站队列的消息按 channel 分发给订阅者。"""
        while True:
            msg = await self.out_queue.dequeue()
            subscriber = self._out_subscribers.get(msg.channel)
            if subscriber is None:
                logger.warning("No channel subscribed for %r, message dropped", msg.channel)
                continue
            await subscriber.enqueue(msg)

    async def run_stream_dispatcher(self) -> None:
        """后台任务：把中央流式事件队列按 channel 分发给订阅者。"""
        while True:
            event = await self.stream_queue.dequeue()
            subscriber = self._stream_subscribers.get(event.channel)
            if subscriber is None:
                logger.warning("No channel subscribed for %r, message dropped", event.channel)
                continue
            await subscriber.enqueue(event)

