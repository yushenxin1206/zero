import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass

from zero.bus.queue import MessageBus
from zero.core.hooks import HookChain, Hooks
from zero.core.loop import AgentLoop, TurnFinishedHook
from zero.channels.cli import CLIChannel
from zero.channels.feishu import FeishuChannel
from zero.channels.feishu.control import FeishuController
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
from zero.config import Config, ConfigManager
from zero.data_paths import DataPaths
from zero.models import CHANNEL_FEISHU
from zero.permission.envelope import PermissionState
from zero.scheduler.store import SchedulerStore
from zero.session.store import SessionStore
from zero.utils.log_setup import setup_logging

log = logging.getLogger(__name__)

# 子 agent 拿到的能力与主对话同源，仅剔除递归 subagent（避免无限委派）。
# shell 等需人工审批的操作在后台无人值守，由 permission.guard 对 subagent 来源
# 直接 fail-closed（预置 zero-permission add-root 可长期放行），不会挂起等审批超时。
SUBAGENT_EXCLUDED_TOOLS = frozenset({"subagent"})


def build_tool_registry(session_store: SessionStore,
                        scheduler_store: SchedulerStore,
                        permission: PermissionState) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(Read())
    registry.register(Write())
    registry.register(Edit())
    registry.register(Shell(permission=permission))
    registry.register(CreateScheduledTask(store=scheduler_store))
    registry.register(CancelScheduledTask(store=scheduler_store))
    return registry


def build_hooks(msg_bus: MessageBus,
                permission: PermissionState) -> Hooks:
    """组合根：构建 hook 链并挂上权限守卫。

    守卫装不上（import 失败、install 提前返回）时 hook 链为空，shell
    权限会静默 fail-open。这里在启动期硬失败，而不是等第一条危险命令。
    """
    chain = HookChain()
    from zero.permission.guard import install
    # msg_bus 供 CLI 就地审批路由；permission 提供探针、扩权表与策略文件
    install(chain, msg_bus, permission=permission)
    assert chain.handlers_for("before_tool_call"), \
        "permission guard 未注册到 hook 链：shell 权限将 fail-open"
    return chain


def build_agent_loop(msg_bus: MessageBus,
                     tool_registry: ToolRegistry,
                     hooks: Hooks,
                     config: Config,
                     session_store: SessionStore,
                     data_paths: DataPaths,
                     sandbox_available: bool,
                     permission: PermissionState | None = None,
                     on_turn_finished: TurnFinishedHook | None = None) -> AgentLoop:
    return AgentLoop(
        msg_bus=msg_bus,
        config=config,
        session_store=session_store,
        data_paths=data_paths,
        sandbox_available=sandbox_available,
        tool_registry=tool_registry,
        hooks=hooks,
        on_turn_finished=on_turn_finished,
        permission=permission,
    )


def build_maintenance_engine(
        config: Config, session_store: SessionStore,
        data_paths: DataPaths) -> MaintenanceEngine:
    return MaintenanceEngine(
        config=config, session_store=session_store, data_paths=data_paths)


@dataclass
class CoreRuntime:
    """一个进程内的核心组件组合；CLI / web 两种启动方式共用。"""
    msg_bus: MessageBus
    session_store: SessionStore
    scheduler_store: SchedulerStore
    permission: PermissionState
    tool_registry: ToolRegistry
    hooks: Hooks
    subagent_manager: SubagentManager
    agent_loop: AgentLoop
    scheduler: Scheduler
    maintenance: MaintenanceEngine


def build_core(config: Config, data_paths: DataPaths) -> CoreRuntime:
    msg_bus = MessageBus()
    session_store = SessionStore(data_paths.sessions)
    scheduler_store = SchedulerStore(data_paths.tasks)
    permission = PermissionState(data_paths, session_store)
    # 能力事实在组合根结算一次：核心层只接收布尔事实，不持有权限对象、
    # 也不在构建提示词时触发探针
    sandbox_available = permission.sandbox_available()
    registry = build_tool_registry(session_store, scheduler_store, permission)
    hooks = build_hooks(msg_bus, permission)
    # 子 agent 管理器：工具集与主对话同源（仅剔除递归 subagent），hooks 共用
    # 同一条链——permission.guard 对子 agent 的需审批命令直接 fail-closed
    subagent_manager = SubagentManager(
        msg_bus=msg_bus,
        config=config,
        session_store=session_store,
        data_paths=data_paths,
        sandbox_available=sandbox_available,
        tool_registry=registry.subset(exclude=SUBAGENT_EXCLUDED_TOOLS),
        hooks=hooks,
    )
    registry.register(SubagentTool(subagent_manager))
    # 定时任务的终态由轮次真正结束时回写：dispatch 只标 running，
    # Scheduler.finish_turn 负责落 completed/failed/cancelled
    scheduler = Scheduler(msg_bus=msg_bus, store=scheduler_store)
    agent_loop = build_agent_loop(
        msg_bus, tool_registry=registry, hooks=hooks,
        config=config, session_store=session_store, data_paths=data_paths,
        sandbox_available=sandbox_available,
        permission=permission,
        on_turn_finished=scheduler.finish_turn)
    return CoreRuntime(
        msg_bus=msg_bus,
        session_store=session_store,
        scheduler_store=scheduler_store,
        permission=permission,
        tool_registry=registry,
        hooks=hooks,
        subagent_manager=subagent_manager,
        agent_loop=agent_loop,
        scheduler=scheduler,
        maintenance=build_maintenance_engine(
            config, session_store, data_paths),
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


def build_cli_channel(core: CoreRuntime, config: Config,
                      data_paths: DataPaths) -> CLIChannel:
    return CLIChannel(
        msg_bus=core.msg_bus, session_store=core.session_store,
        scheduler_store=core.scheduler_store,
        data_paths=data_paths, permission=core.permission,
        config=config, tool_registry=core.tool_registry)


def start_feishu(msg_bus: MessageBus, config: Config,
                 session_store: SessionStore) -> asyncio.Task | None:
    feishu_cfg = config.channels.feishu
    if not feishu_cfg.app_id or not feishu_cfg.app_secret:
        log.info("未配置飞书 appId / appSecret，跳过 feishu channel")
        return None
    channel = FeishuChannel(
        config=feishu_cfg, session_store=session_store, msg_bus=msg_bus)

    async def _serve() -> None:
        try:
            # channel.start() 内部建立 WS 收消息 + bus sender 循环发回复
            await channel.start()
        finally:
            # headless 退出（Ctrl+C / CLI 退出）时也要真正断开长连接
            await channel.stop()

    return asyncio.create_task(_guard(_serve(), CHANNEL_FEISHU))


async def run(config: Config, data_paths: DataPaths, *,
              cli: bool, feishu: bool):
    """headless 模式：按参数启动 CLI / 飞书通道（可同时启动）。

    ``zero --cli``、``zero --feishu``、``zero --cli --feishu`` 都走这里；
    没选 CLI 时进程由核心循环维持，Ctrl+C 退出。
    """
    core = build_core(config, data_paths)
    background = start_background(core)
    if feishu:
        task = start_feishu(core.msg_bus, config, core.session_store)
        if task is not None:
            background.append(task)

    cli_task = None
    if cli:
        cli_task = asyncio.create_task(
            build_cli_channel(core, config, data_paths).start())
    core_task = asyncio.create_task(core.agent_loop.run())

    # CLI 退出（/quit）或核心 loop 结束时结束进程；只开飞书时等 Ctrl+C
    wait_set = {core_task} | ({cli_task} if cli_task is not None else set())
    done: set[asyncio.Task] = set()
    try:
        done, _ = await asyncio.wait(
            wait_set, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in background:
            t.cancel()
        await core.subagent_manager.shutdown()

    task_names = {core_task: "agent-loop", cli_task: "cli-channel"}
    for t in done:
        if not t.cancelled() and (exc := t.exception()) is not None:
            log.error("%s task failed", task_names.get(t, "unknown"), exc_info=exc)


def run_web_desktop(config: Config, data_paths: DataPaths,
                    config_manager: ConfigManager | None = None, *,
                    with_cli: bool = False, with_feishu: bool = False):
    """主线程跑 pywebview GUI；asyncio 后端在 pywebview 的工作线程里运行。

    ``with_cli`` / ``with_feishu`` 决定是否附带启动对应通道；零个时只开 web。
    """
    from zero.channels.web.shell import run_desktop

    web_cfg = config.channels.web

    async def backend(holder: dict):
        from zero.channels.web.server import WebChannel

        core = build_core(config, data_paths)
        background = start_background(core)
        feishu_cfg = config.channels.feishu
        feishu = FeishuController(
            feishu_cfg, core.session_store, core.msg_bus, config_manager)

        cli_task = None
        if with_cli:
            cli_task = asyncio.create_task(
                build_cli_channel(core, config, data_paths).start())

        channel = WebChannel(
            msg_bus=core.msg_bus, session_store=core.session_store,
            scheduler_store=core.scheduler_store, data_paths=data_paths,
            permission=core.permission, config=config,
            config_manager=config_manager, feishu=feishu)
        holder["stop"] = channel.stop_event
        if holder.get("closed"):
            channel.stop_event.set()

        core_task = asyncio.create_task(core.agent_loop.run())
        serve_task = asyncio.create_task(channel.start())
        tasks = {core_task, serve_task}
        if cli_task is not None:
            tasks.add(cli_task)
        try:
            try:
                await channel.wait_ready()
            except Exception as exc:
                log.exception("web channel failed to start")
                holder["error"] = f"web 服务启动失败：{exc}"
                return
            holder.update(
                ready=True, wsUrl=channel.ws_url, token=channel.token)
            # 配置里开着（或本次显式 --feishu）就建连；界面开关随时可改
            await feishu.start_initial(with_feishu or feishu_cfg.enabled)

            done, _ = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED)
            names = {core_task: "agent-loop", serve_task: "web-channel",
                     cli_task: "cli-channel"}
            for t in done:
                if not t.cancelled() and (exc := t.exception()) is not None:
                    log.error("%s task failed", names.get(t, "unknown"),
                              exc_info=exc)
        finally:
            # 窗口关闭后必须等所有任务真正收尾再关闭事件循环，
            # 否则会留下 "Task was destroyed but it is pending" 告警
            await feishu.shutdown()
            pending = background + [core_task, serve_task]
            if cli_task is not None:
                pending.append(cli_task)
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
        # 标题栏只保留系统按钮，不显示文字
        title="",
        width=web_cfg.width,
        height=web_cfg.height,
        theme_path=data_paths.root / "web-theme",
    )


def parse_args():
    parser = argparse.ArgumentParser(
        description="zero agent（不带参数时默认只启动 web 桌面窗口）")
    parser.add_argument(
        "--web", action="store_true",
        help="启动 web 桌面窗口（默认）",
    )
    parser.add_argument(
        "--cli", action="store_true",
        help="启动 CLI 通道",
    )
    parser.add_argument(
        "--feishu", action="store_true",
        help="启动飞书通道（需在配置里填写 appId / appSecret）",
    )

    args = parser.parse_args()
    # 没有显式选择任何通道时默认 web；多个开关可同时生效
    if not (args.web or args.cli or args.feishu):
        args.web = True
    return args


def main():
    args = parse_args()

    # 组合根：唯一的路径配置点 + 唯一一次配置读取。之后所有组件接收
    # 显式 Config / DataPaths / Store，不再访问模块级单例。
    data_paths = DataPaths.default()
    # 用户技能根预先建好：包内技能只读，agent 新增技能应落到这里；
    # 目录存在后 shell 的 mkdir 才只需要技能根自身可写。
    data_paths.skills.mkdir(parents=True, exist_ok=True)
    # ConfigManager 负责状态判定与后续 Web 端读写：缺配置/坏配置都不抛，
    # 由 Web 引导页接管；CLI 没有引导页，稍后给出可操作的提示。
    manager = ConfigManager(data_paths.config)
    config = manager.load()

    # 日志脚手架：文件 DEBUG（2MB × 3 轮转）+ 控制台 ERROR（不打断 rich
    # 渲染），统一带密钥脱敏；日志固定落在数据根的 logs 目录，任意目录启动
    # 都不会散落 debug.log
    setup_logging(data_paths.logs)

    try:
        if args.web:
            # 配置未就绪时只开界面走引导，CLI / 飞书等无配置无法工作
            ready = not manager.needs_setup
            run_web_desktop(
                config, data_paths, manager,
                with_cli=args.cli and ready,
                with_feishu=args.feishu and ready,
            )
        elif manager.needs_setup:
            print(
                "尚未完成配置：请先运行 `zero`（默认打开桌面窗口）完成首次配置，"
                f"或编辑 {data_paths.config}（可先执行 "
                "`python -m zero.config --write` 生成骨架）。",
                file=sys.stderr,
            )
            return 1
        else:
            feishu_cfg = config.channels.feishu
            wants_feishu = args.feishu or feishu_cfg.enabled
            configured = bool(
                feishu_cfg.app_id.strip() and feishu_cfg.app_secret.strip())
            if wants_feishu and not configured:
                print("未配置飞书 appId / appSecret，跳过 feishu 通道",
                      file=sys.stderr)
                # 显式 --feishu 却缺凭据仍按原行为退出；只是配置里开着则
                # 继续启动界面，让用户在「飞书」面板里看到「未配置」并补配置
                if args.feishu and not args.cli:
                    return 1
            asyncio.run(run(
                config, data_paths, cli=args.cli,
                feishu=wants_feishu and configured))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
