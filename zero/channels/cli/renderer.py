"""
Terminal rendering for the CLI channel.

Everything that touches rich lives here: panels, the Live streaming frame,
overflow trimming for long replies, history/banner/session tables, and the
asynchronous result panels produced by background turns. ``CLIChannel``
decides *what* to show; this module decides *how* it looks.
"""
from __future__ import annotations

import sys

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.formatted_text import ANSI as PtkANSI
from prompt_toolkit.shortcuts import print_formatted_text

from zero.channels.service import ChannelService, SessionView
from zero.models import Message

TRUNCATED = "...truncated"

# Live 帧允许占用的终端行数上限（含上下边框）：为状态行、输入提示符、光标行
# 各留一格，避免帧顶到屏幕边界后 rich 触发 ellipsis 裁剪（见 commit_overflow）。
_FRAME_RESERVED_LINES = 3
_FRAME_MIN_LINES = 6


class CliRenderer:
    """rich-based presentation layer for one terminal channel."""

    def __init__(self, console: Console, service: ChannelService):
        self.console = console
        self.service = service

    # ------------------------------------------------------------------
    # Primitives
    # ------------------------------------------------------------------

    @staticmethod
    def clip(text: str, max_len: int) -> str:
        return text if len(text) <= max_len else text[:max_len] + TRUNCATED

    def panel(self, renderable, **kwargs) -> Panel:
        """构造宽度 = 终端宽度 - 1 的 Panel。

        末列留余量：一些终端的 overlay 滚动条会遮住最后一列，且部分
        终端对 emoji 的单元格宽度计算与 rich 不一致，右边框贴边时
        容易看起来"没有闭合"。
        """
        kwargs.setdefault("width", max(20, self.console.width - 1))
        return Panel(renderable, **kwargs)

    def assistant_panel(self, text: str):
        body: object = Markdown(text) if text else Text("(thinking ...)", style="dim")
        return self.panel(
            body, title="zero",
            title_align="left", border_style="green",
        )

    def reasoning_panel(self, text: str):
        """思考过程面板：dim 边框 + dim 正文，视觉上降级于正式回答。"""
        body = (Text(text, style="dim") if text
                else Text("(thinking ...)", style="dim"))
        return self.panel(
            body, title="思考中", title_align="left", border_style="dim")

    # ------------------------------------------------------------------
    # Live frame and overflow control
    # ------------------------------------------------------------------

    def frame(self, acc: str, status: str, reasoning: str = ""):
        """构造 Live 帧：状态行 / 思考面板 / 正文面板，按需出现。

        与历史渲染共用 assistant_panel，保证新消息与历史消息样式一致。
        reasoning 为空时行为与旧版完全一致（仅状态行 + 正文）。
        """
        parts: list = []
        if status:
            # 用静态状态行替代动画 Spinner：帧内容在两次事件之间保持不变，
            # 因此无需按帧高频重绘，从根上消除重绘擦除错位导致的重复显示
            parts.append(Text(status, style="green"))
        if reasoning:
            # 思考面板只展示最新尾部（reasoning 是过渡信息、不落盘，无需回看
            # 完整推理；限高避免思考很长时把帧顶出屏幕触发 ellipsis 裁剪）
            cap = max(4, self.frame_budget() - 4)
            parts.append(self.reasoning_panel(self.tail_fit(reasoning, cap)))
        if acc:
            parts.append(self.assistant_panel(acc))
        elif not reasoning:
            # 无正文也无思考时的占位（思考 chunk 到达前的空窗期）
            parts.append(self.assistant_panel(""))
        if len(parts) == 1:
            return parts[0]
        return Group(*parts)

    def tail_fit(self, text: str, max_lines: int) -> str:
        """截取 text 的尾部（前面补省略提示），使其渲染不超过 max_lines 行。"""
        if max_lines <= 0:
            return text
        if self.measure(Text(text, style="dim")) <= max_lines:
            return text
        lo, hi, best = 0, len(text), len(text)
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.measure(Text(text[mid:], style="dim")) <= max_lines:
                best, hi = mid, mid - 1
            else:
                lo = mid + 1
        tail = text[best:]
        nl = tail.find("\n")
        if 0 <= nl < len(tail) - 1:
            tail = tail[nl + 1:]  # 对齐行首，避免从一行中间开始展示
        return "…\n" + tail

    def collapse_reasoning(self, live: Live, reasoning: str) -> None:
        """把已展示的思考过程折叠成一行摘要。

        正文开始 / 工具调用 / 审批之前调用：思考过程是过渡性内容，收起后
        避免占据并污染正式输出。与 freeze_paragraph 同法——先清空 Live 帧，
        再把摘要打印到 Live 区之上。
        """
        if not reasoning.strip():
            return
        live.update(self.frame("", ""), refresh=True)
        self.console.print(
            Text(f"💭 思考 {len(reasoning.strip())} 字（已折叠）", style="dim"))

    def frame_budget(self) -> int:
        """Live 帧允许占用的最大行数（含上下边框）。"""
        try:
            height = self.console.height
        except Exception:
            height = 24
        return max(_FRAME_MIN_LINES, height - _FRAME_RESERVED_LINES)

    def measure(self, renderable) -> int:
        """把 renderable 渲染成行并返回行数（不裁剪，故可判断是否超屏）。"""
        try:
            return len(self.console.render_lines(renderable,
                                                 self.console.options, pad=False))
        except Exception:
            return 0  # 测量失败一律视为「放得下」，退化为原 ellipsis 行为

    def fit_prefix(self, text: str, budget: int) -> int:
        """返回能完整容纳于 budget 行的最长前缀长度（尽量切在换行处）。"""
        lo, hi, best = 1, len(text), 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.measure(self.frame(text[:mid], "")) <= budget:
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        if best <= 0:
            return 0
        if best < len(text):
            newline = text.rfind("\n", 0, best)
            if newline > 0:
                return newline + 1  # 对齐到行首，避免把一行从中间劈开
        return best

    def commit_overflow(self, live: Live, acc: str) -> str:
        """把超出终端高度的顶部文本固化到滚动区，返回保留在帧内的尾部。

        Live 帧用 vertical_overflow="ellipsis"：帧一旦高于终端，rich 会把
        内容裁剪为「前 N 行 + ...」（保留顶部、丢掉底部），于是最新流出的文本
        看不见，屏幕看起来「卡住」，直到收尾 stop() 才整体刷出（观感为突然跳
        变）。这里改成：帧行数超过终端高度时，把顶部整段打印到 Live 区之上
        （终端随之向上滚动），帧内只保留最新尾部。这样输出连续下滚、最新文本
        始终可见，且收尾时 stop() 只补印尾部、不会重复。
        """
        budget = self.frame_budget()
        if not acc or self.measure(self.frame(acc, "")) <= budget:
            return acc
        committed: list[str] = []
        while acc and self.measure(self.frame(acc, "")) > budget:
            cut = self.fit_prefix(acc, budget)
            if cut <= 0:
                break  # 无法切分（极端超宽单行），交由 ellipsis 兜底
            committed.append(acc[:cut])
            acc = acc[cut:]
        if committed:
            # 先把帧收缩到剩余尾部，再把固化段打印到帧上方。顺序反了会让旧帧
            # （仍含固化段）被 hook 重绘一次，出现重复。
            live.update(self.frame(acc, ""), refresh=True)
            self.console.print(self.assistant_panel("".join(committed)))
        return acc

    def freeze_paragraph(self, live: Live, acc: str) -> None:
        """把已累积的模型文本固化为静态段落（Live 帧之外），帧清零。

        工具调用/审批等间隙调用：已输出的文本只出现一次，后续帧从空累积，
        避免旧文本被反复重绘造成「重复自述」的观感；与历史共用
        assistant_panel，固化段落样式与历史消息一致。

        这里刻意不用 stop()/start()：stop() 会把 vertical_overflow 永久改成
        "visible"（rich 为保证终帧完整渲染），使后续帧高度重新不受终端限制、
        重绘时擦除行数与屏幕不符而产生重复；且 stop() 自身会打印当前帧，与
        下面的显式打印叠加成两份。改为先清空 Live 帧，再把段落打印到 Live
        区之上（rich 会自动把它插到 live 区上方）。
        """
        live.update(self.frame("", ""), refresh=True)
        self.console.print(self.assistant_panel(acc))

    # ------------------------------------------------------------------
    # History and session lists
    # ------------------------------------------------------------------

    def print_banner(self, session_id: str, title: str) -> None:
        title_part = f" · [bold]{escape(title)}[/bold]" if title else ""
        self.console.print(self.panel(
            "[bold]zero[/bold] · cli channel\n"
            f"session [dim]{session_id[:8]}[/dim]{title_part} · "
            "commands: /new /sessions /title /tasks /bgstream /history /help /quit",
            border_style="cyan",
        ))

    def session_table(self, sessions: list[SessionView], current_id: str) -> Table:
        table = Table(title="历史会话 (channel=cli)", expand=True)
        table.add_column("#", style="bold", justify="right")
        table.add_column("日期")
        table.add_column("最近更新")
        table.add_column("标题")
        table.add_column("会话 ID", style="dim")
        for i, s in enumerate(sessions[:20], 1):
            marker = " [bold green]← 当前[/]" if s.session_id == current_id else ""
            title = (f"[dim]{escape(s.title)}[/dim]" if s.title_is_derived
                     else escape(s.title)) if s.title else "[dim]-[/dim]"
            table.add_row(
                str(i),
                s.date,
                s.last_updated_at.strftime("%Y-%m-%d %H:%M:%S"),
                title,
                s.session_id[:13] + marker,
            )
        return table

    def print_message_line(self, message: Message, max_len: int = 600) -> None:
        # 压缩摘要（load_history 合成的消息）：独立样式，避免被误读为用户发言
        if message.metadata.get("kind") == "compact":
            self.console.print(
                f"[dim magenta]compact ›[/dim magenta] "
                f"{self.clip(message.body.content or '', max_len)}")
            return
        if message.sender == "user":
            self.console.print(
                f"[bold cyan]you ›[/bold cyan] "
                f"{self.clip(message.body.content or '', max_len)}")
        elif message.sender == "assistant" and message.body.content:
            self.console.print(
                self.assistant_panel(
                    self.clip(message.body.content, max_len)))

    def print_recent_history(self, session_id: str, limit: int = 6) -> None:
        history = self.service.history(session_id)
        if not history:
            return
        shown = [m for m in history if m.sender in ("user", "assistant")]
        self.console.print(
            f"[dim]—— 最近 {min(len(shown), limit)} 条历史消息 ——[/dim]")
        for message in shown[-limit:]:
            self.print_message_line(message)

    def print_full_history(self, session_id: str) -> None:
        history = self.service.history(session_id)
        if not history:
            self.console.print("[dim]当前会话暂无消息[/dim]")
            return
        # 只显示 user / assistant 会话内容，过滤 tool 等内部过程数据
        lines = [m for m in history if m.sender != "tool"]
        if sys.platform == "win32" or not self.console.is_terminal:
            # Windows 下 console.pager() 走 pydoc tempfile_pager，用本地代码页
            # (GBK) 写临时文件再过 more.com："›" 不在 GBK 中，会被
            # backslashreplace 成字面量 r"\u203a"。非终端（管道 / 测试）下
            # pager 同样会绕过 console.file 直写 stdout，直接输出更符合预期，
            # 翻页交给终端 scrollback。
            self._print_history_lines(lines)
            return
        with self.console.pager():
            self._print_history_lines(lines)

    def _print_history_lines(self, history: list[Message]) -> None:
        for message in history:
            self.print_message_line(message, max_len=100000)

    # ------------------------------------------------------------------
    # Asynchronous (background turn) output
    # ------------------------------------------------------------------

    def print_above_prompt(self, renderable, *, prompt_active: bool,
                           end: str = "\n") -> None:
        """Print a rich renderable above the active input prompt.

        提示符活跃时不走 patch_stdout 代理写原始 ANSI：那条路径曾经把
        转义序列转义成 "?[2m" 乱码（raw=False），raw 直通又绕过了 ptk
        渲染器的属性状态管理，导致面板颜色状态不闭合（黄色"流"出边框）。
        这里把 rich 渲染结果捕获为 ANSI 字符串，交给 ptk 自己的
        print_formatted_text 管线重新渲染——它首尾都会显式
        reset_attributes，保证颜色闭合；run_in_terminal 负责在提示符
        上方输出并重绘提示符。提示符不活跃时直接打印到真实 stdout。
        """
        if prompt_active:
            with self.console.capture() as capture:
                self.console.print(renderable, end=end)
            ansi = capture.get()
            run_in_terminal(
                lambda: print_formatted_text(PtkANSI(ansi), end=""),
                in_executor=False,
            )
        else:
            self.console.print(renderable, end=end)

    def render_async_record(self, rec: dict, *, prompt_active: bool) -> None:
        """Render one completed background turn / notification record."""
        subtitle_parts = []
        title = self.service.title(rec["session_id"])
        if title:
            subtitle_parts.append(f"会话「{escape(title)}」")
        if rec["source"] == "scheduler" and rec["task_id"]:
            task = self.service.task(rec["task_id"])
            if task is not None:
                instruction = task.instruction.removeprefix("[Scheduled Task]")
                subtitle_parts.append(escape(self.clip(instruction, 40)))
        subtitle = " · ".join(subtitle_parts) or None
        print_above = lambda renderable: self.print_above_prompt(  # noqa: E731
            renderable, prompt_active=prompt_active)

        if rec.get("kind") == "subagent_result":
            print_above(self.panel(
                Markdown(rec["text"]), title=f"zero · 子任务 {rec['task_id']}",
                title_align="left", border_style="magenta", subtitle=subtitle))
        elif rec.get("kind") == "approval_blocked":
            print_above(self.panel(
                Markdown(rec["text"]), title="zero · 审批未受理",
                title_align="left", border_style="red", subtitle=subtitle))
        elif rec["error"]:
            # 出错标题按来源区分：此前写死「定时任务出错」，导致子任务回投轮
            # 或用户后台轮出错时被错标成定时任务
            error_title = {
                "scheduler": "zero · 定时任务出错",
                "subagent": "zero · 子任务出错",
            }.get(rec["source"], "zero · 后台回答出错")
            print_above(self.panel(
                escape(rec["text"]), title=error_title,
                title_align="left", border_style="red", subtitle=subtitle))
        elif rec["source"] == "scheduler":
            if rec["streamed"]:
                # /bgstream 模式下正文已流式输出，只补一行完成提示
                print_above(
                    "\n[green]✅ 定时任务完成[/green]"
                    + (f" [dim]{subtitle}[/dim]" if subtitle else ""))
                return
            print_above(self.panel(
                Markdown(rec["text"]), title="zero · 定时任务",
                title_align="left", border_style="yellow", subtitle=subtitle))
        else:
            print_above(self.panel(
                Markdown(rec["text"]), title="zero · 后台回答",
                title_align="left", border_style="blue", subtitle=subtitle))
