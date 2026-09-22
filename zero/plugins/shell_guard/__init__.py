"""
shell_guard 插件：shell 命令三态守卫（allow / deny / ask）。

设计方案见 docs/shell_permission.md（v4）与 docs/hooks.md。
核心零策略；本插件提供全部判定、审批与规则沉淀逻辑。
"""
from zero.bus.queue import MessageBus
from zero.core.hooks import HookChain
from zero.plugins.shell_guard.handler import ShellGuard


def install(hooks: HookChain, msg_bus: MessageBus | None = None) -> ShellGuard:
    """组合根入口：把 shell_guard 注册进 hook 链。

    msg_bus 用于 CLI 就地审批（审批请求经 stream 队列路由）；
    不传则 ask 一律 fail-closed。
    """
    guard = ShellGuard(msg_bus)
    hooks.on_before_tool_call(guard)
    return guard
