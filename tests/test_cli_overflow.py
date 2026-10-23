"""Regression test: 流式渲染超过终端高度时必须「连续下滚」而非截断卡住。

背景（修复前的缺陷）
-------------------
流式渲染把整段累积文本塞进一个 rich ``Live`` 帧，并用
``vertical_overflow="ellipsis"`` 防止帧高溢出。但 rich 的 ellipsis 策略是
「保留顶部 N 行 + ...」，于是当模型输出超过一屏时：屏幕停在最早那几行、
最新文本被裁掉；观感像「卡住」，直到收尾才突然跳出全文。

修复后由 ``CliRenderer.commit_overflow()`` 负责：帧行数超过终端高度时，
把顶部整段打印到 Live 区之上（终端随之向上滚动），帧内只保留最新尾部。

本文件锁定三条不变量：
  1. **不丢字**——固化到滚动区的内容 + 帧内尾部 == 原文；
  2. **不重复**——固化段与帧内尾部无重叠；
  3. **帧高受控**——帧始终保持 ≤ 预算，故 ellipsis 裁剪永不触发。
"""
import io
import re

from rich.console import Console

from zero.channels.cli.renderer import CliRenderer, _FRAME_MIN_LINES

ANSI = re.compile(r"\x1b\[[0-9;]*m")
BOX = re.compile(r"[\u2500-\u257f]")  # 面板边框/分隔线字符


class FakeLive:
    """记录 update 调用，避免依赖真实终端。"""

    def __init__(self):
        self.refreshes = 0

    def update(self, renderable, refresh=False):
        if refresh:
            self.refreshes += 1


def squash(s: str) -> str:
    """去掉 ANSI、空白与边框字符，便于做内容归属判断。"""
    return re.sub(r"\s+", "", BOX.sub("", ANSI.sub("", s)))


def make_renderer(width: int = 80, height: int = 24) -> CliRenderer:
    """构造只带 Console 的渲染器（帧逻辑不需要 service）。"""
    return CliRenderer(
        Console(width=width, height=height, file=io.StringIO(), highlight=False),
        service=None)


def committed_output(ch: CliRenderer) -> str:
    return ch.console.file.getvalue()


def check(ch: CliRenderer, text: str, label: str) -> None:
    """对单次调用检验三条不变量。"""
    buf = ch.console.file
    buf.seek(0)
    buf.truncate(0)
    live = FakeLive()

    tail = ch.commit_overflow(live, text)
    head = text[: len(text) - len(tail)]

    # 1. 不丢字：尾部必须是原文后缀（拼接等于原文，顺序未乱）
    assert text.endswith(tail), f"{label}: 帧内尾部不是原文后缀"
    assert head + tail == text, f"{label}: 固化 + 尾部 != 原文"

    # 3. 帧高受控：帧内尾部必须放得下
    assert ch.measure(ch.frame(tail, "")) <= ch.frame_budget(), \
        f"{label}: 帧内尾部仍超预算"

    if head:
        # 2a. 固化段确实被打印到滚动区
        assert squash(head) in squash(committed_output(ch)), \
            f"{label}: 固化段未出现在滚动区输出"
        # 2b. 不重复：固化段不应同时留在帧内尾部
        assert squash(head) not in squash(tail), \
            f"{label}: 固化段与帧内尾部重叠（会重复显示）"
        # 有固化就必须有过重绘（把帧收缩到尾部）
        assert live.refreshes >= 1, f"{label}: 固化后未重绘帧"
    else:
        # 无需固化时不得产生多余打印/重绘
        assert live.refreshes == 0, f"{label}: 无需固化却重绘了帧"
        assert committed_output(ch) == "", f"{label}: 无需固化却打印了内容"


def test_short_text_not_committed():
    check(make_renderer(), "简短回答，一行就够。", "短文本")


def test_empty_text_returns_immediately():
    ch = make_renderer()
    assert ch.commit_overflow(FakeLive(), "") == ""
    assert committed_output(ch) == ""


def test_long_text_commits_prefix():
    long_text = "\n\n".join(
        f"第{i}段：" + "这是一段用于测试的中文内容，" * 8 for i in range(1, 40))
    check(make_renderer(), long_text, "长文本(多段)")


def test_unbroken_line_hard_cuts():
    check(make_renderer(), "内容" * 2500, "超长单行")


def test_narrow_terminal_still_progresses():
    narrow = make_renderer(width=24, height=8)
    assert narrow.frame_budget() >= _FRAME_MIN_LINES
    check(narrow, "窄屏" * 400, "窄终端")


def test_char_by_char_stream_roundtrips():
    """逐字符模拟流式：全程拼接可完整还原原文。"""
    ch = make_renderer()
    doc = "".join(
        f"第{i}段：" + "流式累积测试。" * 10 + "\n\n" for i in range(1, 30))
    acc = ""
    chunks: list[str] = []
    live = FakeLive()
    for char in doc:
        acc += char
        shorter = ch.commit_overflow(live, acc)
        if len(shorter) != len(acc):
            chunks.append(acc[: len(acc) - len(shorter)])
        acc = shorter

    assert "".join(chunks) + acc == doc, "流式累积后无法完整还原原文"
    assert ch.measure(ch.frame(acc, "")) <= ch.frame_budget(), "终态帧超预算"
