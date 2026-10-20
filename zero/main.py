import argparse
import asyncio
import logging

from zero.bus.queue import MessageBus
from zero.core.hooks import HookChain, Hooks
from zero.core.loop import AgentLoop
from zero.channels.cli import CLIChannel
from zero.channels.feishu import FeishuChannel
from zero.maintenance import MaintenanceEngine
from zero.scheduler import Scheduler
from zero.subagent import SubagentManager
from zero.tools.registry import ToolRegistry
from zero.tools.read import Read
from zero.tools.write import Write
from zero.tools.edit import Edit
from zero.tools.shell import Shell
from zero.tools.scheduler_task import CancelScheduledTask, CreateScheduledTask
from zero.tools.subagent import SubagentTool
from zero.config import ZERO_ROOT, default_config

log = logging.getLogger(__name__)

# 子 agent 拿到的能力与主对话同源，仅剔除递归 subagent（避免无限委派）。
# shell 等需人工审批的操作在后台无人值守，由 permission.guard 对 subagent 来源
# 直接 fail-closed（预置 zero-permission add-root 可长期放行），不会挂起等审批超时。
SUBAGENT_EXCLUDED_TOOLS = frozenset({"subagent"})


def build_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(Read())
    registry.register(Write())
    registry.register(Edit())
    registry.register(Shell())
    registry.register(CreateScheduledTask())
    registry.register(CancelScheduledTask())
    return registry


def build_hooks(msg_bus: MessageBus) -> Hooks:
    """组合根：构建 hook 链并挂上权限守卫。无守卫时是纯 no-op 链，
    运行时行为与无 hooks 逐字节一致。"""
    chain = HookChain()
    from zero.permission.guard import install
    install(chain, msg_bus)     # msg_bus 供 CLI 就地审批路由
    return chain


def build_agent_loop(msg_bus: MessageBus,
                     tool_registry: ToolRegistry,
                     hooks: Hooks) -> AgentLoop:
    return AgentLoop(
        msg_bus=msg_bus,
        config=default_config,
        tool_registry=tool_registry,
        hooks=hooks,
    )


def build_maintenance_engine() -> MaintenanceEngine:
    return MaintenanceEngine(config=default_config)


async def run(cli_only: bool = False):
    """
    启动 zero agent。默认同时拉起 CLI 和飞书两个 channel，
    共享同一个 MessageBus / AgentLoop / Scheduler。
    """
    msg_bus = MessageBus()
    registry = build_tool_registry()
    hooks = build_hooks(msg_bus)
    # 子 agent 管理器：工具集与主对话同源（仅剔除递归 subagent），hooks 共用
    # 同一条链——permission.guard 对子 agent 的需审批命令直接 fail-closed
    subagent_manager = SubagentManager(
        msg_bus=msg_bus,
        config=default_config,
        tool_registry=registry.subset(exclude=SUBAGENT_EXCLUDED_TOOLS),
        hooks=hooks,
    )
    registry.register(SubagentTool(subagent_manager))
    agent_loop = build_agent_loop(msg_bus, tool_registry=registry, hooks=hooks)
    scheduler = Scheduler(msg_bus=msg_bus)

    maintenance_engine = build_maintenance_engine()

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
        asyncio.create_task(guard(maintenance_engine.run(), "maintenance")),
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

    cli_channel = CLIChannel(
        msg_bus=msg_bus, config=default_config, tool_registry=registry)
    core = asyncio.create_task(agent_loop.run())
    cli_task = asyncio.create_task(cli_channel.start())

    # CLI 退出（/quit）或核心 loop 结束时，取消所有后台任务，
    # 并取消在途子 agent（其回投消息已无会话可送达）
    done, pending = await asyncio.wait(
        {core, cli_task}, return_when=asyncio.FIRST_COMPLETED)
    for t in background:
        t.cancel()
    await subagent_manager.shutdown()
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
    log_dir = ZERO_ROOT
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
