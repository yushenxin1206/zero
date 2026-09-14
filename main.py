import argparse
import asyncio
import logging
from pathlib import Path

from bus.queue import MessageBus
from core.loop import AgentLoop
from channels.cli import CLIChannel
from channels.feishu import FeishuChannel
from scheduler import Scheduler
from tools.registry import ToolRegistry
from tools.read import Read
from tools.write import Write
from tools.edit import Edit
from tools.shell import Shell
from tools.scheduler_task import CancelScheduledTask, CreateScheduledTask
from config import default_config


def build_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(Read())
    registry.register(Write())
    registry.register(Edit())
    registry.register(Shell())
    registry.register(CreateScheduledTask())
    registry.register(CancelScheduledTask())
    return registry


def build_agent_loop(msg_bus: MessageBus) -> AgentLoop:
    return AgentLoop(
        msg_bus=msg_bus,
        runtime_config=default_config.agent_runtime,
        provider_configs=default_config.providers,
        tool_registry=build_tool_registry(),
    )


async def run(cli_only: bool = False):
    """
    启动 zero agent。默认同时拉起 CLI 和飞书两个 channel，
    共享同一个 MessageBus / AgentLoop / Scheduler。
    """
    log = logging.getLogger(__name__)
    msg_bus = MessageBus()
    agent_loop = build_agent_loop(msg_bus)
    scheduler = Scheduler(msg_bus=msg_bus)

    async def guard(coro, name: str):
        """运行后台协程；单个 channel 崩溃只记日志，不拖垮整体。"""
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s crashed", name)

    background: list[asyncio.Task] = [
        asyncio.create_task(guard(scheduler.run(), "scheduler")),
        asyncio.create_task(guard(msg_bus.run_out_dispatcher(), "out-dispatcher")),
        asyncio.create_task(guard(msg_bus.run_stream_dispatcher(), "stream-dispatcher")),
    ]

    if not cli_only:
        feishu_cfg = default_config.channels.feishu
        if feishu_cfg.app_id:
            channel = FeishuChannel(config=feishu_cfg, msg_bus=msg_bus)
            # channel.start() 内部启动 WS 线程收消息 + bus sender 循环发回复
            background.append(asyncio.create_task(guard(channel.start(), "feishu")))
        else:
            log.warning("未配置飞书 app_id，跳过 feishu channel")

    cli_channel = CLIChannel(msg_bus=msg_bus)
    core = asyncio.create_task(agent_loop.run())
    cli_task = asyncio.create_task(cli_channel.start())

    # CLI 退出（/quit）或核心 loop 结束时，取消所有后台任务
    done, pending = await asyncio.wait(
        {core, cli_task}, return_when=asyncio.FIRST_COMPLETED)
    for t in background:
        t.cancel()
    task_names = {core: "agent-loop", cli_task: "cli-channel"}
    for t in done:
        if not t.cancelled() and (exc := t.exception()) is not None:
            log.error("%s task failed", task_names.get(t, "unknown"), exc_info=exc)


def parse_args():
    parser = argparse.ArgumentParser(description="zero agent")
    parser.add_argument(
        "--cli", action="store_true",
        help="只运行 CLI 通道，不连接飞书（默认两个 channel 同时启动）",
    )
    return parser.parse_args()


def quiet_third_party_loggers():
    """第三方库自带的 stdout handler 会绕过日志文件配置直接污染终端，
    一律移除并改为汇入 root logging（写入 debug.log）。"""
    for name in ("Lark", "websocket"):
        lib_logger = logging.getLogger(name)
        lib_logger.handlers.clear()
        lib_logger.propagate = True


def main():
    args = parse_args()

    # 日志统一写文件（绝对路径，任意目录启动都不会散落 debug.log），
    # 避免打断 rich 渲染的终端界面
    log_dir = Path.home() / ".zero"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(log_dir / "debug.log"),
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    quiet_third_party_loggers()

    try:
        asyncio.run(run(cli_only=args.cli))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
