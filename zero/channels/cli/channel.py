"""
CLI channel — lifecycle shell.

What stays here is only what is genuinely channel-wide:
  * start/stop and the interactive chat loop
  * turn demultiplexing: which output belongs to the active turn and which
    to the background pipeline
  * SIGINT semantics (Ctrl+C during a render stops the turn, not the app)
  * the Live render loop for the active turn

Collaborators:

    ChannelService   sessions / history / tasks / models / turns   (shared)
    CliRenderer      panels, Live frames, overflow, history views
    CliInput         prompt_toolkit session, unread badge, fallback input
    CliApproval      generic approval panel + key reading
    CliCommands      slash commands and the session picker
    CliBackground    background turns, rendered async above the prompt

Input runs in a worker thread (blocking input()) while the event loop keeps
consuming stream events, so rendering stays live.
"""
from __future__ import annotations

import asyncio
import signal
import time

from rich.console import Console
from rich.live import Live

from zero.channels.base import BusChannel
from zero.channels.cli.approval import CliApproval
from zero.channels.cli.background import CliBackground
from zero.channels.cli.commands import CliCommands
from zero.channels.cli.input import CliInput
from zero.channels.cli.renderer import CliRenderer
from zero.channels.service import ChannelService
from zero.config import Config
from zero.models import MessageOut, StreamEvent
from zero.session.store import SessionStore
from zero.scheduler.store import SchedulerStore
from zero.tools.registry import ToolRegistry

CHANNEL = "cli"

# 流式渲染的最小重绘间隔（秒）。Live 每次重绘都要「擦除整帧 + 重画整帧」，
# 若每个 token 都触发（可达几十次/秒），终端上会持续闪烁；固定到这个帧率后
# 重绘次数与 token 速率解耦，观感稳定。
_RENDER_INTERVAL = 0.08


class CLIChannel(BusChannel):
    """Interactive CLI channel."""

    name = CHANNEL

    def __init__(self, msg_bus, session_store: SessionStore,
                 scheduler_store: SchedulerStore,
                 config: Config | None = None,
                 tool_registry: ToolRegistry | None = None,
                 console: Console | None = None):
        super().__init__(msg_bus)
        self.console = console or Console(highlight=False)
        self.service = ChannelService(
            CHANNEL, msg_bus=msg_bus, session_store=session_store,
            scheduler_store=scheduler_store,
            config=config, tool_registry=tool_registry)
        self.renderer = CliRenderer(self.console, self.service)
        self.input = CliInput(self.console, toolbar=self._toolbar_line)
        self.approval = CliApproval(
            self.console, msg_bus.approvals, use_ptk=self.input.uses_ptk)
        self.commands = CliCommands(
            self.console, self.service, self.renderer, self.input)

        # ---- turn demultiplexing state (standing consumers) ----
        # turn currently rendered by _render_turn; set before the MessageIn
        # is enqueued so early events cannot be misrouted to the bg pipeline
        self._active_turn_id = ""
        # events / final MessageOut belonging to the active interactive turn
        self._turn_events: asyncio.Queue[StreamEvent] = asyncio.Queue()
        self._turn_out: asyncio.Queue[MessageOut] = asyncio.Queue()
        # serializes Live rendering and async-result printing so a scheduled
        # result never tears into an active streaming frame
        self._render_lock = asyncio.Lock()
        self.background = CliBackground(
            self.renderer, self.input, self._render_lock,
            stream_enabled=lambda: self.commands.bg_stream_enabled)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        """Entry point: pick/restore session, then run the chat loop."""
        if self.service.restore_binding():
            title = self.service.title()
            title_part = f"「{title}」" if title else ""
            self.console.print(
                f"[dim]已恢复会话 {self.service.session_id[:8]}{title_part}"
                "（输入 /sessions 可切换）[/dim]")
            if not self.commands.confirm_or_switch():
                return
        else:
            if not self.commands.pick_session():
                return

        self.commands.greet_current()
        self._install_sigint_handler()

        # 常驻出站消费者（BusChannel）+ 异步结果渲染 worker。定时任务等
        # 后台 turn 的结果不再依赖用户发消息才被看到。
        self.start_consumers()
        self.spawn(self.background.run())
        try:
            await self._chat_loop()
        finally:
            await self.stop_consumers()

    async def stop(self):
        await self.stop_consumers()

    def _toolbar_line(self) -> str:
        """常驻底部状态行：展示本会话当前生效模型（channel 无关查询）。"""
        model = self.service.current_model() or "?"
        return (f" model: {model} "
                f"· session: {self.service.session_id[:8]}")

    # ------------------------------------------------------------------
    # Chat loop
    # ------------------------------------------------------------------

    async def _chat_loop(self):
        while True:
            try:
                # Ctrl+C / Ctrl+D 均视为退出
                user_text = await self.input.read()
            except (EOFError, KeyboardInterrupt):
                self.console.print("\n[dim]bye[/dim]")
                return

            text = (user_text or "").strip()
            self.input.mark_read()  # 用户已回到终端，清零未读指示
            if not text:
                continue
            if text.startswith("/"):
                if not await self.commands.dispatch(text):
                    return
                continue

            msg = self.service.make_input(text)
            # 先入激活 turn 再入队，保证 listener 不会把本轮最早的事件
            # 误判成后台 turn
            self._active_turn_id = msg.id
            await self.msg_bus.in_queue.enqueue(msg)
            # 流式中 Ctrl+C 由 _render_turn 捕获并转为终止请求；空闲时
            # Ctrl+C（prompt_async 抛 KeyboardInterrupt）走上面的退出分支
            await self._render_turn(msg.id)

    # ------------------------------------------------------------------
    # SIGINT：turn 渲染中 Ctrl+C = 终止当前 turn（不退出应用）
    # ------------------------------------------------------------------

    def _install_sigint_handler(self):
        """接管 SIGINT，堵住「渲染中 Ctrl+C 直接退出整个应用」的洞。

        不接管时的路径（Windows / 无 loop signal 支持的平台）：
        KeyboardInterrupt 从 run_forever 抛出 → asyncio Runner 取消主
        task（run()）→ 它的 asyncio.wait 结束并连锁取消全部任务 →
        _chat_loop 的 CancelledError 分支根本没机会执行 → 应用整体退出。
        更糟的是取消直穿 runner 工具循环时曾留下孤儿 tool_calls（400）。

        接管后的语义：
        - turn 渲染中   → 转终止请求，应用不退（这就是「对话中终止」）
        - ptk 输入会话  → ptk raw 模式下 Ctrl+C 不产生 SIGINT（键事件由
                          ptk 处理 → KeyboardInterrupt → 现有 bye 路径），
                          handler 不会被调到；保险起见直接 return
        - 其余空闲      → SystemExit 传播，维持退出语义
        """
        def handler(signum, frame):
            if self.input.active:
                return
            turn_id = self._active_turn_id
            if not turn_id:
                raise SystemExit(130)
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            loop.call_soon_threadsafe(self._sigint_stop_turn, turn_id)

        try:
            signal.signal(signal.SIGINT, handler)
        except ValueError:
            # 非主线程（测试环境）：装不了信号处理器，维持默认行为
            pass

    def _sigint_stop_turn(self, turn_id: str):
        """在事件循环线程里把 SIGINT 转成终止请求。

        不取消 _render_turn：渲染循环继续等事件，runner 断流后
        cancelled done 事件自然到达，由其 cancelled 分支展示
        「已终止（以上为已生成内容）」。

        审批面板若在倒计时也一并通知收工：SIGINT 路径不取消任何
        task，CliApproval.handle 的 finally 不会执行，倒计时线程会
        孤儿运行到 180s 超时（曾表现为「审批时 Ctrl+C 终止不了」）。
        """
        asyncio.ensure_future(self.service.request_stop(turn_id))
        self.approval.abort_pending()

    # ------------------------------------------------------------------
    # Bus consumption: demultiplex outbound traffic by turn_id
    # ------------------------------------------------------------------

    async def deliver(self, message: MessageOut) -> None:
        if message.turn_id and message.turn_id == self._active_turn_id:
            await self._turn_out.put(message)
        else:
            self.background.deliver(message)

    async def deliver_stream(self, event: StreamEvent) -> None:
        if event.turn_id and event.turn_id == self._active_turn_id:
            await self._turn_events.put(event)
        else:
            self.background.handle_event(event)

    # ------------------------------------------------------------------
    # Interactive turn rendering
    # ------------------------------------------------------------------

    async def _render_turn(self, turn_id: str):
        """Render one interactive turn live, consuming events that
        deliver_stream routed into this turn's private queue."""
        renderer = self.renderer
        acc = ""
        reason_acc = ""   # 本轮实时思考文本（仅展示，不参与最终回复、不落盘）
        status = ""
        usage: dict | None = None
        last_render = 0.0  # 上次实际重绘的 monotonic 时刻（节流用）
        # vertical_overflow="ellipsis"：帧高度裁剪到终端高度以内，保证重绘时
        # 擦除的行数始终与屏幕一致（"visible" 下帧高于终端会擦除错位、叠出
        # 重复帧）。auto_refresh=False：状态行是静态文本，改为事件到达时按需
        # refresh，去掉后台高频重绘。
        live = Live(
            renderer.frame(acc, status),
            console=self.console,
            auto_refresh=False,
            vertical_overflow="ellipsis",
        )
        done = False
        cancelled = False
        try:
            # 持证渲染：定时任务结果等异步输出会等本轮 Live 结束后再打印，
            # 避免撕裂流式画面
            async with self._render_lock:
                # refresh=True：立即画出首帧（空正文 → "(thinking ...)" 占位），
                # 否则 auto_refresh=False 下要等第一个内容 delta 才渲染，思考期
                # 屏幕上会长时间空白（reasoning chunk 当前不透传，首个 delta 往往
                # 在整个思考阶段之后才到）
                live.start(refresh=True)
                while not done:
                    event = await self._turn_events.get()
                    if event.turn_id != turn_id:
                        # 防御：listener 已按 active turn 路由，正常不会走到
                        continue
                    if event.kind == "delta":
                        # 正文开始 → 收起思考面板（折叠成一行摘要），
                        # 让正式回答独占画面
                        if reason_acc.strip():
                            renderer.collapse_reasoning(live, reason_acc)
                            reason_acc = ""
                        acc += event.content
                        # 工具已结束、模型继续输出：清除残留的工具状态行，
                        # 否则它会留在每一帧乃至终帧里重复出现
                        status = ""
                    elif event.kind == "reasoning":
                        # 思考 chunk：仅累积作实时展示（dim 面板），
                        # 不进 acc，故不影响最终回复、不落盘
                        reason_acc += event.content
                    elif event.kind == "tool_call":
                        # 思考结束、转入工具调用：先收思考再固化正文
                        if reason_acc.strip():
                            renderer.collapse_reasoning(live, reason_acc)
                            reason_acc = ""
                        # 固化已累积文本后清零帧：否则该段落会随后续帧反复
                        # 重绘，观感像模型在「重复自述」（实际只输出一次）
                        if acc.strip():
                            renderer.freeze_paragraph(live, acc)
                            acc = ""
                        status = f"调用工具 {renderer.clip(event.content, 90)}"
                    elif event.kind == "approval":
                        # 固化段落后再审批：审批面板停/启 Live 会整帧重绘，
                        # 旧文本若留在帧里会在审批后再次出现。先固化清帧，
                        # 审批后只显示新文本
                        if reason_acc.strip():
                            renderer.collapse_reasoning(live, reason_acc)
                            reason_acc = ""
                        if acc.strip():
                            renderer.freeze_paragraph(live, acc)
                            acc = ""
                        # 通用就地审批：暂停 Live 提问，回答回填后恢复渲染
                        await self.approval.handle(event, live)
                    elif event.kind == "error":
                        status = ""
                        reason_acc = ""
                        live.stop()
                        self.console.print(f"[red]出错:[/red] {event.content}")
                        done = True  # 视为终结，迟到的 MessageOut 不再重复渲染
                    elif event.kind == "done":
                        status = ""
                        reason_acc = ""
                        if event.content and not acc:
                            acc = event.content
                        if event.token_usage is not None:
                            usage = event.token_usage
                        done = True
                        if event.cancelled:
                            cancelled = True
                    # 节流重绘：Live 每次重绘都要「擦除整帧 + 重画整帧」，若每个
                    # token 都刷（可达几十次/秒）终端会持续闪烁。仅在：回合终结、
                    # 队列已空（这一串事件的最后一个，保证尾帧不丢）、或距上次
                    # 重绘超过 _RENDER_INTERVAL 时才真正重绘；否则只登记内容，
                    # 交给下一次重绘一并画出。
                    now = time.monotonic()
                    if (done or self._turn_events.empty()
                            or now - last_render >= _RENDER_INTERVAL):
                        # 先把超出终端高度的顶部固化到滚动区，保证帧内始终是
                        # 最新文本、终端随输出连续下滚（而非被 ellipsis 裁掉底部）
                        acc = renderer.commit_overflow(live, acc)
                        live.update(
                            renderer.frame(acc, status, reason_acc),
                            refresh=True)
                        last_render = now
                    else:
                        live.update(renderer.frame(acc, status, reason_acc))
        except asyncio.CancelledError:
            # 用户终止（流式中按 Ctrl+C）：请求核心真正停止生成（不是甩开
            # UI 转后台），并展示已生成片段 + 「已终止」。标记 finalized
            # 让收尾的 done 流事件与 MessageOut 走后台管线时被丢弃，避免
            # 部分回复被当作「后台回答」二次渲染
            cancelled = True
            await self.service.request_stop(turn_id)
            # 审批工作线程的收工信号已由 CliApproval.handle 的 finally 置位
            # （本 except 晚于该 finally 执行，approval._abort 已清空）
            self.background.mark_finalized(turn_id)
        finally:
            live.stop()
            # 正常终结（done/error）的 turn：迟到的 MessageOut 由后台管线丢弃
            if done or cancelled:
                self.background.mark_finalized(turn_id)
            if self._active_turn_id == turn_id:
                self._active_turn_id = ""
            if usage:
                self.console.print(
                    f"[dim]tokens: {usage.get('promptTokens', 0)} in / "
                    f"{usage.get('completionTokens', 0)} out / "
                    f"{usage.get('totalTokens', 0)} total[/dim]"
                )
            self.console.print()
        # 终止提示：已生成片段已在 Live 终帧展示，这里补一行状态
        if cancelled:
            self.console.print(
                "[yellow]已终止[/yellow]" + (
                    "（以上为已生成内容）" if acc else ""))

        # Drain the matching final MessageOut so queues stay in sync.
        try:
            await asyncio.wait_for(self._turn_out.get(), timeout=2)
        except (asyncio.TimeoutError, TimeoutError):
            pass
