"""
Message Queue Class
"""
import asyncio
import logging
from typing import TypeVar, Generic

from abc import ABC
from zero.models import Base, MessageIn, MessageOut, StopRequest, StreamEvent

T = TypeVar('T', bound=Base)

logger = logging.getLogger(__name__)

# 每条队列的容量上限。数字不是性能调参，而是「积压必须在有限步内暴露」：
# 入站最紧（串行消费，积压直接等于用户等待）；出站允许一小段突发；流式
# 事件可以更多，但同样必须有界。满了以后怎么办由生产者在入队时显式选择。
IN_CAPACITY = 64
OUT_CAPACITY = 256
STREAM_CAPACITY = 1024
STOP_CAPACITY = 16


class BusQueueFull(RuntimeError):
    """入队被拒绝：队列已达容量上限，消息未被接受。

    调用方不能把它当成「已提交」继续往下走；必须向自己的上游反馈
    （忙线提示、下轮重试，或等待空位）。
    """

    def __init__(self, name: str, capacity: int):
        super().__init__(f"{name} 队列已满（capacity={capacity}）")
        self.name = name
        self.capacity = capacity


class MessageQueue(ABC, Generic[T]):
    """带容量上限的总线队列。

    三种入队语义，调用方必须显式选择——队列不知道业务上「丢了要不要紧」：

    * ``enqueue``        等待空位（背压）。用于不可再生、必须送达的生产者。
    * ``enqueue_nowait`` 满时抛 ``BusQueueFull``。用于边界入口，由边界向
                         用户反馈（忙线提示 / 下轮重试）。
    * ``enqueue_latest`` 满时先丢弃最旧一条。只用于 dispatcher → channel
                         的展示队列：最新状态优先，且绝不阻塞核心链路。
    """

    def __init__(self, name: str, capacity: int):
        if capacity <= 0:
            raise ValueError(f"队列 {name} 的容量必须为正数：{capacity}")
        self.name = name
        self.capacity = capacity
        self.queue: asyncio.Queue[T] = asyncio.Queue(maxsize=capacity)

    async def enqueue(self, message: T) -> None:
        """等待空位后入队（背压）。"""
        await self.queue.put(message)

    def enqueue_nowait(self, message: T) -> None:
        """非阻塞入队；满时抛 ``BusQueueFull``，消息未被接受。"""
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            raise BusQueueFull(self.name, self.capacity) from None

    def enqueue_latest(self, message: T) -> T | None:
        """非阻塞入队；满时丢弃最旧一条腾位，返回被丢弃项（无则 None）。"""
        try:
            self.queue.put_nowait(message)
            return None
        except asyncio.QueueFull:
            dropped = self.queue.get_nowait()
            self.queue.put_nowait(message)
            return dropped

    async def dequeue(self) -> T:
        return await self.queue.get()

    def clear(self) -> None:
        """丢弃队列中所有待处理项；用于通道停用后避免恢复时补发旧消息。"""
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    @property
    def size(self) -> int:
        return self.queue.qsize()


class MessageInQueue(MessageQueue[MessageIn]):
    def __init__(self, capacity: int = IN_CAPACITY):
        super().__init__("in", capacity)


class MessageOutQueue(MessageQueue[MessageOut]):
    def __init__(self, capacity: int = OUT_CAPACITY, name: str = "out"):
        super().__init__(name, capacity)


class StreamEventQueue(MessageQueue[StreamEvent]):
    def __init__(self, capacity: int = STREAM_CAPACITY, name: str = "stream"):
        super().__init__(name, capacity)


class StopRequestQueue(MessageQueue[StopRequest]):
    def __init__(self, capacity: int = STOP_CAPACITY):
        super().__init__("stop", capacity)


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
    只消费自己的消息。所有队列都有容量上限，满队列行为见 ``MessageQueue``。
    """

    def __init__(
            self,
            *,
            in_capacity: int = IN_CAPACITY,
            out_capacity: int = OUT_CAPACITY,
            stream_capacity: int = STREAM_CAPACITY,
            stop_capacity: int = STOP_CAPACITY,
    ):
        self._out_capacity = out_capacity
        self._stream_capacity = stream_capacity
        self.in_queue = MessageInQueue(in_capacity)
        self.out_queue = MessageOutQueue(out_capacity)
        self.stream_queue = StreamEventQueue(stream_capacity)
        # 终止信号旁路：AgentLoop 后台协程消费；不占 in_queue
        self.stop_queue = StopRequestQueue(stop_capacity)
        self.approvals = ApprovalBroker()
        self._out_subscribers: dict[str, MessageOutQueue] = {}
        self._stream_subscribers: dict[str, StreamEventQueue] = {}

    def subscribe_out(self, channel: str) -> MessageOutQueue:
        """登记并返回 channel 专属的出站队列（幂等）。"""
        if channel not in self._out_subscribers:
            self._out_subscribers[channel] = MessageOutQueue(
                self._out_capacity, name=f"out:{channel}")
        return self._out_subscribers[channel]

    def subscribe_stream(self, channel: str) -> StreamEventQueue:
        """登记并返回 channel 专属的流式事件队列（幂等）。"""
        if channel not in self._stream_subscribers:
            self._stream_subscribers[channel] = StreamEventQueue(
                self._stream_capacity, name=f"stream:{channel}")
        return self._stream_subscribers[channel]

    async def run_out_dispatcher(self) -> None:
        """后台任务：把中央出站队列的消息按 channel 分发给订阅者。

        dispatcher 是枢纽，任何 channel 的积压都不能反压它：订阅队列满时
        丢弃最旧一条并记 error（终稿本身已落盘，重载历史即可恢复）。
        """
        while True:
            msg = await self.out_queue.dequeue()
            subscriber = self._out_subscribers.get(msg.channel)
            if subscriber is None:
                logger.warning("No channel subscribed for %r, message dropped", msg.channel)
                continue
            dropped = subscriber.enqueue_latest(msg)
            if dropped is not None:
                logger.error(
                    "channel %r 出站积压超限（capacity=%d），丢弃最旧消息 id=%s",
                    msg.channel, subscriber.capacity, dropped.id)

    async def run_stream_dispatcher(self) -> None:
        """后台任务：把中央流式事件队列按 channel 分发给订阅者。

        同 ``run_out_dispatcher``：丢弃最旧事件而不是阻塞核心。最终正文
        由 done 事件与落盘的 MessageOut 承载，中间 delta 丢失只影响实时
        观感。
        """
        while True:
            event = await self.stream_queue.dequeue()
            subscriber = self._stream_subscribers.get(event.channel)
            if subscriber is None:
                logger.warning("No channel subscribed for %r, message dropped", event.channel)
                continue
            dropped = subscriber.enqueue_latest(event)
            if dropped is not None:
                logger.error(
                    "channel %r 流式积压超限（capacity=%d），丢弃最旧事件 kind=%s turn_id=%s",
                    event.channel, subscriber.capacity,
                    dropped.kind, dropped.turn_id)
