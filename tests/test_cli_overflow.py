"""Regression test: 流式渲染超过终端高度时必须「连续下滚」而非截断卡住。

背景（修复前的缺陷）
-------------------
流式渲染把整段累积文本塞进一个 rich ``Live`` 帧，并用
``vertical_overflow="ellipsis"`` 防止帧高溢出。但 rich 的 ellipsis 策略是
「保留顶部 N 行 + ...」（``rich/live_render.py``: ``lines[: height - 1]``），
于是当模型输出超过一屏时：

* 屏幕始终停在最早那几行，**最新文本被裁掉、看不见**；
* 观感上像是「卡住不动」，直到本轮 ``Live.stop()`` 把 overflow 改成
  ``"visible"`` 整体刷一次，才突然跳出全文。

修复后由 ``CLIChannel._commit_overflow()`` 负责：帧行数超过终端高度时，把顶部
整段打印到 Live 区之上（终端随之向上滚动），帧内只保留最新尾部。

本文件锁定三条不变量：
  1. **不丢字**——固化到滚动区的内容 + 帧内尾部 == 原文（顺序不乱、不缺失）；
  2. **不重复**——固化段与帧内尾部无重叠；
  3. **帧高受控**——帧始终保持 ≤ 预算，故 ellipsis 裁剪永不触发。

Run: python tests/test_cli_overflow.py
"""
import io
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console  # noqa: E402

from zero.channels.cli import CLIChannel, _FRAME_MIN_LINES  # noqa: E402

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


def make_channel(width: int = 80, height: int = 24) -> CLIChannel:
    """构造只带 Console 的 CLIChannel（跳过 __init__ 的 IO/订阅副作用）。"""
    obj = CLIChannel.__new__(CLIChannel)
    obj.console = Console(width=width, height=height,
                          file=io.StringIO(), highlight=False)
    return obj


def committed_output(ch: CLIChannel, live: FakeLive) -> str:
    return ch.console.file.getvalue()


def check(ch: CLIChannel, text: str, label: str) -> None:
    """对单次调用检验三条不变量。"""
    buf = ch.console.file
    buf.seek(0)
    buf.truncate(0)
    live = FakeLive()

    tail = ch._commit_overflow(live, text)
    head = text[: len(text) - len(tail)]

    # 1. 不丢字：尾部必须是原文后缀（保证拼接等于原文，且顺序未被打乱）
    assert text.endswith(tail), f"{label}: 帧内尾部不是原文后缀"
    assert head + tail == text, f"{label}: 固化 + 尾部 != 原文"

    # 3. 帧高受控：帧内尾部必须放得下
    assert ch._measure(ch._frame(tail, "")) <= ch._frame_budget(), \
        f"{label}: 帧内尾部仍超预算"

    if head:
        # 2a. 固化段确实被打印到滚动区
        assert squash(head) in squash(committed_output(ch, live)), \
            f"{label}: 固化段未出现在滚动区输出"
        # 2b. 不重复：固化段不应同时留在帧内尾部
        assert squash(head) not in squash(tail), \
            f"{label}: 固化段与帧内尾部重叠（会重复显示）"
        # 有固化就必须有过重绘（把帧收缩到尾部）
        assert live.refreshes >= 1, f"{label}: 固化后未重绘帧"
    else:
        # 无需固化时不得产生多余打印/重绘
        assert live.refreshes == 0, f"{label}: 无需固化却重绘了帧"
        assert committed_output(ch, live) == "", f"{label}: 无需固化却打印了内容"

    print(f"[OK] {label}: 原文 {len(text)} 字 → 固化 {len(head)} / "
          f"帧内 {len(tail)}（帧 {ch._measure(ch._frame(tail, ''))} 行 / "
          f"预算 {ch._frame_budget()}）")


# ---------------------------------------------------------------------------
# 1. 基本行为
# ---------------------------------------------------------------------------
ch = make_channel(width=80, height=24)
print(f"console {ch.console.width}x{ch.console.height}, "
      f"预算 {ch._frame_budget()} 行\n")

# 短文本：放得下，不得固化
check(ch, "简短回答，一行就够。", "短文本")

# 空文本：原地返回
assert ch._commit_overflow(FakeLive(), "") == ""
print("[OK] 空文本: 直接返回")

# 长文本（多段）：应固化顶部、帧内留尾部
long_text = "\n\n".join(
    f"第{i}段：" + "这是一段用于测试的中文内容，" * 8 for i in range(1, 40))
check(ch, long_text, "长文本(多段)")

# 超长单行（无换行）：只能硬切
check(ch, "内容" * 2500, "超长单行")

# 极端窄终端：预算存在下限，仍能推进（不得死循环）
narrow = make_channel(width=24, height=8)
assert narrow._frame_budget() >= _FRAME_MIN_LINES
check(narrow, "窄屏" * 400, "窄终端")


# ---------------------------------------------------------------------------
# 2. 逐字符模拟流式：全程拼接可完整还原原文
# ---------------------------------------------------------------------------
ch2 = make_channel()
doc = "".join(f"第{i}段：" + "流式累积测试。" * 10 + "\n\n" for i in range(1, 30))
acc = ""
chunks: list[str] = []
live = FakeLive()
for ch_ in doc:
    acc += ch_
    shorter = ch2._commit_overflow(live, acc)
    if len(shorter) != len(acc):
        chunks.append(acc[: len(acc) - len(shorter)])
    acc = shorter

assert "".join(chunks) + acc == doc, "流式累积后无法完整还原原文"
assert ch2._measure(ch2._frame(acc, "")) <= ch2._frame_budget(), "终态帧超预算"
print(f"\n[OK] 逐字符流式: 原文 {len(doc)} 字，固化 {len(chunks)} 段，"
      f"终态帧内 {len(acc)} 字，完整还原=True")

print("\nALL CLI OVERFLOW TESTS PASSED")
