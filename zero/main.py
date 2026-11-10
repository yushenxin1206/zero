import argparse
import asyncio
import logging
from dataclasses import dataclass

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
from zero.config import Config, load_config
from zero.paths import Paths, configure
from zero.scheduler.store import SchedulerStore
from zero.session.store import SessionStore
from zero.utils.log_setup import setup_logging

log = logging.getLogger(__name__)

# 子 agent 拿到的能力与主对话同源，仅剔除递归 subagent（避免无限委派）。
# shell 等需人工审批的操作在后台无人值守，由 permission.guard 对 subagent 来源
# 直接 fail-closed（预置 zero-permission add-root 可长期放行），不会挂起等审批超时。
SUBAGENT_EXCLUDED_TOOLS = frozenset({"subagent"})


def build_tool_registry(session_store: SessionStore,
                        scheduler_store: SchedulerStore) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(Read())
    registry.register(Write())
    registry.register(Edit())
    registry.register(Shell(session_store=session_store))
    registry.register(CreateScheduledTask(store=scheduler_store))
    registry.register(CancelScheduledTask(store=scheduler_store))
    return registry


def build_hooks(msg_bus: MessageBus, session_store: SessionStore) -> Hooks:
    """组合根：构建 hook 链并挂上权限守卫。

    守卫装不上（import 失败、install 提前返回）时 hook 链为空，shell
    权限会静默 fail-open。这里在启动期硬失败，而不是等第一条危险命令。
    """
    chain = HookChain()
    from zero.permission.guard import install
    # msg_bus 供 CLI 就地审批路由；session_store 供包络推导会话目录
    install(chain, msg_bus, session_store=session_store)
    assert chain.handlers_for("before_tool_call"), \
        "permission guard 未注册到 hook 链：shell 权限将 fail-open"
    return chain


def build_agent_loop(msg_bus: MessageBus,
                     tool_registry: ToolRegistry,
                     hooks: Hooks,
                     config: Config,
                     session_store: SessionStore) -> AgentLoop:
    return AgentLoop(
        msg_bus=msg_bus,
        config=config,
        session_store=session_store,
        tool_registry=tool_registry,
        hooks=hooks,
    )


def build_maintenance_engine(
        config: Config, session_store: SessionStore) -> MaintenanceEngine:
    return MaintenanceEngine(config=config, session_store=session_store)


@dataclass
class CoreRuntime:
    """一个进程内的核心组件组合；CLI / web 两种启动方式共用。"""
    msg_bus: MessageBus
    session_store: SessionStore
    scheduler_store: SchedulerStore
    tool_registry: ToolRegistry
    hooks: Hooks
    subagent_manager: SubagentManager
    agent_loop: AgentLoop
    scheduler: Scheduler
    maintenance: MaintenanceEngine


def build_core(config: Config, paths: Paths) -> CoreRuntime:
    msg_bus = MessageBus()
    session_store = SessionStore(paths.sessions)
    scheduler_store = SchedulerStore(paths.tasks)
    registry = build_tool_registry(session_store, scheduler_store)
    hooks = build_hooks(msg_bus, session_store)
    # 子 agent 管理器：工具集与主对话同源（仅剔除递归 subagent），hooks 共用
    # 同一条链——permission.guard 对子 agent 的需审批命令直接 fail-closed
    subagent_manager = SubagentManager(
        msg_bus=msg_bus,
        config=config,
        session_store=session_store,
        tool_registry=registry.subset(exclude=SUBAGENT_EXCLUDED_TOOLS),
        hooks=hooks,
    )
    registry.register(SubagentTool(subagent_manager))
    agent_loop = build_agent_loop(
        msg_bus, tool_registry=registry, hooks=hooks,
        config=config, session_store=session_store)
    return CoreRuntime(
        msg_bus=msg_bus,
        session_store=session_store,
        scheduler_store=scheduler_store,
        tool_registry=registry,
        hooks=hooks,
        subagent_manager=subagent_manager,
        agent_loop=agent_loop,
        scheduler=Scheduler(msg_bus=msg_bus, store=scheduler_store),
        maintenance=build_maintenance_engine(config, session_store),
    )


async def _guard(coro, name: str):
    """运行后台协程；单个 channel 崩溃只记日志，不拖垮整体。"""
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("%s crashed", name)


def start_background(core: CoreRuntime) -> list[asyncio.Task]:
    return [
        asyncio.create_task(_guard(core.scheduler.run(), "scheduler")),
        asyncio.create_task(_guard(core.maintenance.run(), "maintenance")),
        asyncio.create_task(_guard(
            core.msg_bus.run_out_dispatcher(), "out-dispatcher")),
        asyncio.create_task(_guard(
            core.msg_bus.run_stream_dispatcher(), "stream-dispatcher")),
    ]


def start_feishu(msg_bus: MessageBus, config: Config,
                 session_store: SessionStore) -> asyncio.Task | None:
    feishu_cfg = config.channels.feishu
    if not feishu_cfg.app_id:
        log.info("未配置飞书 app_id，跳过 feishu channel")
        return None
    channel = FeishuChannel(
        config=feishu_cfg, session_store=session_store, msg_bus=msg_bus)
    # channel.start() 内部启动 WS 线程收消息 + bus sender 循环发回复
    return asyncio.create_task(_guard(channel.start(), "feishu"))


async def run(config: Config, paths: Paths, cli_only: bool = False):
    """
    启动 zero agent。默认同时拉起 CLI 和飞书两个 channel，
    共享同一个 MessageBus / AgentLoop / Scheduler。
    """
    core = build_core(config, paths)
    background = start_background(core)
    if not cli_only:
        if (feishu := start_feishu(
                core.msg_bus, config, core.session_store)) is not None:
            background.append(feishu)

    cli_channel = CLIChannel(
        msg_bus=core.msg_bus, session_store=core.session_store,
        scheduler_store=core.scheduler_store,
        config=config, tool_registry=core.tool_registry)
    core_task = asyncio.create_task(core.agent_loop.run())
    cli_task = asyncio.create_task(cli_channel.start())

    # CLI 退出（/quit）或核心 loop 结束时，取消所有后台任务，
    # 并取消在途子 agent（其回投消息已无会话可送达）
    done, pending = await asyncio.wait(
        {core_task, cli_task}, return_when=asyncio.FIRST_COMPLETED)
    for t in background:
        t.cancel()
    await core.subagent_manager.shutdown()
    task_names = {core_task: "agent-loop", cli_task: "cli-channel"}
    for t in done:
        if not t.cancelled() and (exc := t.exception()) is not None:
            log.error("%s task failed", task_names.get(t, "unknown"), exc_info=exc)


def run_web_desktop(config: Config, paths: Paths):
    """主线程跑 pywebview GUI；asyncio 后端在 pywebview 的工作线程里运行。"""
    from zero.channels.web.shell import run_desktop

    web_cfg = config.channels.web

    async def backend(holder: dict):
        from zero.channels.web.server import WebChannel

        core = build_core(config, paths)
        background = start_background(core)
        if (feishu := start_feishu(
                core.msg_bus, config, core.session_store)) is not None:
            background.append(feishu)

        channel = WebChannel(
            msg_bus=core.msg_bus, session_store=core.session_store,
            scheduler_store=core.scheduler_store, config=config)
        holder["stop"] = channel.stop_event
        if holder.get("closed"):
            channel.stop_event.set()

        core_task = asyncio.create_task(core.agent_loop.run())
        serve_task = asyncio.create_task(channel.start())
        try:
            try:
                await channel.wait_ready()
            except Exception as exc:
                log.exception("web channel failed to start")
                holder["error"] = f"web 服务启动失败：{exc}"
                return
            holder.update(
                ready=True, wsUrl=channel.ws_url, token=channel.token)

            done, _ = await asyncio.wait(
                {core_task, serve_task}, return_when=asyncio.FIRST_COMPLETED)
            names = {core_task: "agent-loop", serve_task: "web-channel"}
            for t in done:
                if not t.cancelled() and (exc := t.exception()) is not None:
                    log.error("%s task failed", names.get(t, "unknown"),
                              exc_info=exc)
        finally:
            # 窗口关闭后必须等所有任务真正收尾再关闭事件循环，
            # 否则会留下 "Task was destroyed but it is pending" 告警
            pending = background + [core_task, serve_task]
            for t in pending:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await core.subagent_manager.shutdown()

            # 兜底：core 内部还有临时任务（如 agent-loop 的 stop 信号消费者、
            # channel 的 worker），它们被各自 finally 取消但未必等到收敛。
            # 事件循环即将关闭，必须全部收尾，否则 GC 时会报
            # "Task was destroyed but it is pending!"
            current = asyncio.current_task()
            leftover = [t for t in asyncio.all_tasks() if t is not current]
            if leftover:
                for t in leftover:
                    t.cancel()
                await asyncio.gather(*leftover, return_exceptions=True)

    run_desktop(
        backend,
        title="zero",
        width=web_cfg.width,
        height=web_cfg.height,
    )


def parse_args():
    parser = argparse.ArgumentParser(description="zero agent")
    parser.add_argument(
        "--cli", action="store_true",
        help="只运行 CLI 通道，不连接飞书（默认两个 channel 同时启动）",
    )
    parser.add_argument(
        "--web", action="store_true",
        help="以 pywebview 桌面窗口运行 web 通道（需安装 .[web] 可选依赖）",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # 组合根：唯一的路径配置点 + 唯一一次配置读取。之后所有组件接收
    # 显式 Config / Paths / Store，不再访问模块级单例。
    paths = configure()
    config = load_config()

    # 日志脚手架：文件 DEBUG（2MB × 3 轮转）+ 控制台 ERROR（不打断 rich
    # 渲染），统一带密钥脱敏；绝对路径，任意目录启动都不会散落 debug.log
    setup_logging(paths.root)

    try:
        if args.web:
            run_web_desktop(config, paths)
        else:
            asyncio.run(run(config, paths, cli_only=args.cli))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
