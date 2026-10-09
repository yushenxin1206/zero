"""Regression test: 模型 reasoning(思考) 输出经 bus 供 channel 消费。

链路：provider 产出 ("reasoning", chunk) → runner 转成
StreamEvent(kind="reasoning") → bus 按 channel 路由 → CLI 实时 dim 面板展示，
正文开始后折叠成一行。设计约束（评审决策）：

  1. 默认展示（CLI 实时渲染思考过程）；
  2. **不落盘**——reasoning 绝不进入最终回复、绝不写会话历史；
  3. 后台 turn（定时任务/已转后台轮次）忽略 reasoning。

本文件锁定三条不变量：
  A. **透传**：runner 把每个 reasoning chunk 原样转为 kind="reasoning" 事件；
  B. **隔离**：reasoning 不混入 delta/done，最终回复（及落盘内容）不含它；
  C. **渲染**：CLI 帧能同时/分别呈现思考与正文，且后台管线忽略 reasoning。

Run: python tests/test_reasoning_stream.py
"""
import asyncio
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console  # noqa: E402
from rich.text import Text  # noqa: E402

from zero.channels.cli import CLIChannel  # noqa: E402
from zero.core.context import SubAgentContext  # noqa: E402
from zero.core.runner import AgentRunner  # noqa: E402
from zero.models import (  # noqa: E402
    AgentRuntimeConfig, LLMCallOptions, LLMRuntime, ProviderConfig, StreamEvent)


class FakeProvider:
    """产出预设的 (kind, data) 流项。"""

    def __init__(self, chunks):
        self.chunks = chunks

    async def stream_chunks(self, payload, stop_event=None):
        for item in self.chunks:
            yield item


def make_runner(chunks) -> AgentRunner:
    """构造只带必要属性的 runner（跳过 __init__ 的配置解析/客户端构造）。"""
    r = AgentRunner.__new__(AgentRunner)
    r.runtime_config = AgentRuntimeConfig(
        llm_call_options=LLMCallOptions(model="mock", max_tokens=100),
        llm_runtime=LLMRuntime(provider=ProviderConfig(),
                               context_window_tokens=20000),
    )
    r.context = SubAgentContext("测试任务")   # compressible=False，跳过压缩
    r.provider = FakeProvider(chunks)
    r._stop_event = None
    r.tool_registry = None
    r.compactor = None
    return r


def collect(runner) -> list:
    async def _run():
        out = []
        async for ev in runner._run_loop():
            out.append(ev)
        return out

    return asyncio.new_event_loop().run_until_complete(_run())


def reply_of(events) -> str:
    """复刻 AgentLoop 的回复累积规则：只认 delta / done。"""
    reply = ""
    for ev in events:
        if ev.kind == "delta":
            reply += ev.content
        elif ev.kind == "done":
            reply = ev.content or reply
    return reply


# ---------------------------------------------------------------------------
# A + B. runner：透传 reasoning，且与正文/最终回复隔离
# ---------------------------------------------------------------------------
events = collect(make_runner([
    ("reasoning", "先想第一点，"),
    ("reasoning", "再想第二点。"),
    ("content", "这是最终答案。"),
    ("usage", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}),
]))

kinds = [e.kind for e in events]
reasoning_text = "".join(e.content for e in events if e.kind == "reasoning")
done_events = [e for e in events if e.kind == "done"]

assert kinds.count("reasoning") == 2, f"应透传 2 个 reasoning 事件，实际 {kinds}"
assert reasoning_text == "先想第一点，再想第二点。", "reasoning 内容未原样透传"
assert len(done_events) == 1 and done_events[0].content == "这是最终答案。", \
    "done 的正文不正确"
assert "想" not in done_events[0].content, "reasoning 混入了 done 正文"
assert reply_of(events) == "这是最终答案。", "最终回复被 reasoning 污染（会落盘）"
print("A/B. runner 透传 + 隔离 ok:", kinds)

# 仅 reasoning、无正文的轮次：不再静默放行空正文，转为报错
only_reasoning = collect(make_runner([("reasoning", "只在思考")]))
assert [e.kind for e in only_reasoning] == ["reasoning", "error"], \
    f"仅 reasoning（空正文）应报错，实际 {[e.kind for e in only_reasoning]}"
assert "只在思考" not in only_reasoning[-1].content, "空正文报错不应带上 reasoning"
print("A/B. 仅 reasoning 轮次 → 报错 ok")


# ---------------------------------------------------------------------------
# A/B2. finish_reason 校验：截断/空正文一律报错，不再静默放行
# ---------------------------------------------------------------------------
# finish_reason=length（思考吃满预算、正文为空）→ error，不产出 done
truncated = collect(make_runner([
    ("reasoning", "很长的思考" * 5),
    ("finish", "length"),
]))
assert [e.kind for e in truncated] == ["reasoning", "error"], \
    f"length 应报错而非 done，实际 {[e.kind for e in truncated]}"
assert "max_tokens" in truncated[-1].content, "报错应说明截断原因"
print("A/B2. finish_reason=length（空正文）→ error ok")

# finish_reason=length 且有部分正文：严格模式同样不放行残缺正文
partial = collect(make_runner([
    ("content", "这是被截断的半句"),
    ("finish", "length"),
]))
assert [e.kind for e in partial] == ["delta", "error"], \
    f"length 有部分正文也应报错，实际 {[e.kind for e in partial]}"
assert not [e for e in partial if e.kind == "done"], "截断不应产出 done"
print("A/B2. finish_reason=length（部分正文）→ error ok")

# finish_reason=stop + 正文 → 正常 done（回归）
normal = collect(make_runner([
    ("content", "完整的答复"),
    ("finish", "stop"),
    ("usage", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}),
]))
assert [e.kind for e in normal] == ["delta", "done"], \
    f"正常收尾不应报错，实际 {[e.kind for e in normal]}"
assert normal[-1].content == "完整的答复", "正常轮次的 done 正文不正确"
print("A/B2. finish_reason=stop → done ok")


# ---------------------------------------------------------------------------
# C1. CLI 渲染：帧组合、占位与尾部裁剪
# ---------------------------------------------------------------------------
def make_channel(width: int = 80, height: int = 24) -> CLIChannel:
    obj = CLIChannel.__new__(CLIChannel)
    obj.console = Console(width=width, height=height,
                          file=io.StringIO(), highlight=False)
    return obj


ch = make_channel()


def render(renderable) -> str:
    ch.console.file.seek(0)
    ch.console.file.truncate(0)
    ch.console.print(renderable)
    return ch.console.file.getvalue()


# 思考面板 + 正文面板同帧
both = render(ch._frame("正文内容", "", "思考过程内容"))
assert "思考过程内容" in both and "正文内容" in both, "思考与正文未同帧呈现"

# 只有思考时，不再显示 "(thinking ...)" 占位
only_reason = render(ch._frame("", "", "思考中のみ"))
assert "思考中のみ" in only_reason and "(thinking ...)" not in only_reason, \
    "有思考时不应再显示占位"

# 无思考无正文 → 保留占位（思考 chunk 到达前的空窗）
placeholder = render(ch._frame("", ""))
assert "(thinking ...)" in placeholder, "空窗期应显示占位"
print("C1. CLI 帧组合 ok")

# 尾部裁剪：超长思考只留尾部并给出省略提示
long_reason = "\n".join(f"第{i}行思考" for i in range(1, 100))
fitted = ch._tail_fit(long_reason, 8)
assert ch._measure(Text(fitted, style="dim")) <= 8, "裁剪后仍超行数上限"
assert fitted.startswith("…"), "裁剪后应带省略提示"
assert fitted.rstrip().endswith("第99行思考"), "应保留最新尾部"
print("C1. 尾部裁剪 ok")


# ---------------------------------------------------------------------------
# C2. 折叠：思考收起为一行摘要
# ---------------------------------------------------------------------------
class FakeLive:
    def __init__(self):
        self.updates = 0

    def update(self, renderable, refresh=False):
        self.updates += 1


ch.console.file.seek(0)
ch.console.file.truncate(0)          # 清掉 C1 的渲染残留
live = FakeLive()
ch._collapse_reasoning(live, "一段思考内容")
buf = ch.console.file.getvalue()
assert "思考" in buf and "已折叠" in buf, "折叠摘要未打印"
assert live.updates >= 1, "折叠时应清空帧（update）"

# 空思考不产生任何输出
before = len(ch.console.file.getvalue())
ch._collapse_reasoning(FakeLive(), "   ")
assert len(ch.console.file.getvalue()) == before, "空思考不应打印摘要"
print("C2. 折叠 ok")


# ---------------------------------------------------------------------------
# C3. 后台管线忽略 reasoning
# ---------------------------------------------------------------------------
bg = CLIChannel.__new__(CLIChannel)
bg._bg_turns = {}
bg._bg_stream_enabled = False
bg._handle_bg_stream_event(
    StreamEvent(kind="reasoning", turn_id="t1", content="后台思考内容"))
acc = bg._bg_turns.get("t1", {})
assert "后台思考内容" not in acc.get("text", ""), "后台 turn 不应累积 reasoning"
bg._handle_bg_stream_event(
    StreamEvent(kind="delta", turn_id="t1", content="后台正文"))
assert bg._bg_turns["t1"]["text"] == "后台正文", "后台正文应正常累积"
print("C3. 后台忽略 reasoning ok")

print("\nALL REASONING STREAM TESTS PASSED")
