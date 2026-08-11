import asyncio
from bus.queue import MessageBus, MessageOutQueue
from config import default_config
from core.loop import AgentLoop
from datacls import Message, MessageBody, MessageIn, MessageOut


async def get_output(out_queue: MessageOutQueue):
    while True:
        msg = await out_queue.dequeue()
        print(msg)


async def main():

    msg_bus = MessageBus()

    agent_loop = AgentLoop(
        msg_bus=msg_bus,
        runtime_config=default_config.agent_runtime,
        provider_configs=default_config.providers,
    )

    await msg_bus.in_queue.enqueue(
        MessageIn(
            body=MessageBody(
                content='Hi',
            ),
        )
    )

    await asyncio.gather(
        agent_loop.run(),
        get_output(msg_bus.out_queue),
    )


if __name__ == "__main__":
    asyncio.run(main())

