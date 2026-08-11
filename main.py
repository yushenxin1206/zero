import asyncio
from bus.queue import MessageBus
from config import default_config
from core.loop import AgentLoop
from datacls import Message, MessageBody


async def main():

    msg_bus = MessageBus()

    agent_loop = AgentLoop(
        msg_bus=msg_bus,
        runtime_config=default_config.agent_runtime,
        provider_configs=default_config.providers,
    )

    await msg_bus.in_queue.enqueue(
        Message(
            id='1',
            sender='1',
            session_id='1',
            recipient='1',
            body=MessageBody(
                content='Hi',
            ),
        )
    )
    return await agent_loop.run()


if __name__ == "__main__":
    print(asyncio.run(main()))

