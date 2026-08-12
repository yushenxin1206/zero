import asyncio
from bus.queue import MessageBus
from core.loop import AgentLoop
from channels.feishu import FeishuChannel
from config import default_config


async def main():
    msg_bus = MessageBus()

    # 飞书长连接通道（自动收消息 + 自动发回复）
    channel = FeishuChannel(
        config=default_config.channels.feishu,
        msg_bus=msg_bus,
    )

    agent_loop = AgentLoop(
        msg_bus=msg_bus,
        runtime_config=default_config.agent_runtime,
        provider_configs=default_config.providers,
    )

    # channel.start() 内部启动 WS 线程收消息 + bus sender 循环发回复
    await asyncio.gather(
        agent_loop.run(),
        channel.start(),
    )

asyncio.run(main())

