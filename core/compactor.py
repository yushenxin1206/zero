"""
会话上下文自动压缩。

每次构建 LLM payload 前调用 Compactor.maybe_compress：当历史估算超出触发
阈值时，把历史前缀交给压缩模型生成摘要并落盘 summary.json；后续
load_history 用 [摘要消息] + 尾部 合成 payload。

设计约束：
- 纯核心实现，不经过 hooks（hook 无法替换已构建的 payload）。
- messages.jsonl 保持 append-only；摘要独立存储，删除 summary.json 即可
  恢复全量历史。
- 压缩失败必须 fail-safe：跳过本轮，继续使用完整历史，只记日志。
"""
import json
import logging
import math
import unicodedata
from typing import Any

from core.context import Context
from models import (
    AgentRuntimeConfig,
    LLMConfig,
    Message,
    SummaryRecord,
)
from providers.base import LLMPayload, Provider
from session.store import SessionStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Token 估算（确定性计算，不走模型）
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：CJK 字符按 1.1、其余按 0.3，向上取整。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if unicodedata.east_asian_width(ch) in ("W", "F"))
    other = len(text) - cjk
    return math.ceil(cjk * 1.1 + other * 0.3)


def estimate_message(m: Message) -> int:
    """估算单条消息的 token 数（含 tool_calls 的 name/arguments）。"""
    total = estimate_tokens(m.body.content or "")
    for tc in m.body.tool_calls or []:
        total += estimate_tokens(tc.function.name) + estimate_tokens(tc.function.arguments)
    return total


def estimate_session_payload(
        system_prompt: str,
        tools: list[dict] | None,
        raw: list[Message],
        rec: SummaryRecord | None,
) -> int:
    """估算当前会话待发 payload 的 token 占用。

    与 estimate_payload(context.messages, context.tools) 口径一致，但直接
    从已加载的全量历史 raw 与摘要记录 rec 合成（[摘要] + 尾部；找不到
    covered_upto 时退化为全量，不计摘要），供 maybe_compress 避免为触发
    判断把 messages.jsonl 再读一遍。
    """
    total = estimate_tokens(system_prompt)
    if tools:
        total += estimate_tokens(json.dumps(tools, ensure_ascii=False))
    tail = raw
    if rec is not None:
        for i, m in enumerate(raw):
            if m.id == rec.covered_upto:
                tail = raw[i + 1:]
                total += estimate_tokens(rec.content)
                break
    return total + sum(estimate_message(m) for m in tail)


def find_tail_boundary(raw: list[Message], tail_budget: int) -> int:
    """返回应保留在 payload 中的尾部起点下标（raw[tail_start:] 为尾部）。

    从末尾向前累加估算，直到超过 tail_budget（软上限，允许越界到完整
    turn）；若边界处的第一条消息是 tool 消息（其对应的 assistant
    tool_call 消息会被压缩掉，产生孤儿 tool），则继续前移边界，保证尾部
    首条消息不是 tool。
    """
    total = 0
    i = len(raw) - 1
    while i >= 0 and total < tail_budget:
        total += estimate_message(raw[i])
        i -= 1
    tail_start = i + 1
    while 0 < tail_start < len(raw) and raw[tail_start].sender == "tool":
        tail_start -= 1
    return tail_start


# ---------------------------------------------------------------------------
# 压缩提示词
# ---------------------------------------------------------------------------

COMPRESS_PROMPT = """Compress this conversation history into a structured summary — the ONLY context for future turns. Output the summary alone, in the conversation's primary language.

Rules:
- A <previous_summary> block, if present, is the sole record of earlier history: merge all its facts, prune what is stale or superseded.
- Record current state and outcomes, not step-by-step transcripts. Keep exact wording only for paths, commands, error text, and names/titles.
- Each fact appears in exactly one section. Empty sections get "(none)"; never omit a heading.
- If space is tight, keep errors/fixes, paths/identifiers, decisions/rationale, pending work — drop tool-call narration first, then Read file entries.

Follow this exact format:

## Goal
[What the user is trying to accomplish]

## Constraints & Preferences
- [Hard constraints and stated preferences; "(none)" if none]

## Progress
### Done
- [x] [Completed tasks/changes — for content-production tasks, include each item's title/topic]
### In Progress
- [ ] [Unfinished work]
### Blocked
- [Issues preventing progress, if any]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Ordered list of remaining work]

## Critical Context
- [Technical facts, exact error text, and details that must not be lost; "(none)" if none]

## Files
Group every file path (absolute or relative to the working directory) by action; each file appears in exactly one group, under its most significant action (deleted > created > modified > read):

### Read
- [files only inspected, never changed]
### Created
- [files newly created]
### Modified
- [pre-existing files edited]
### Deleted
- [files removed]"""

# 压缩调用 max_tokens 额外为 reasoning 思考预留的余量（思考 tokens 与
# 正文共享同一 completion 预算，不留余量时正文会被截断成残句）
REASONING_TOKEN_HEADROOM = 4096

# 以 user 指令收尾：防止历史以 assistant 结尾时，模型把压缩任务误当成
# "续写对话"（把原回复原文当摘要输出的已知失效模式）
SUMMARY_FINAL_INSTRUCTION = (
    "Summarize the conversation above (merging the <previous_summary> block if "
    "present) following the required section headings. Output only the summary — "
    "do not continue the conversation or copy passages verbatim."
)


def build_summary_messages(
        raw_prefix: list[Message],
        prev_summary: SummaryRecord | None,
) -> list[dict]:
    """把待压缩的历史前缀转成压缩模型的输入消息。"""
    messages: list[dict] = [
        {"role": "system", "content": COMPRESS_PROMPT}
    ]
    if prev_summary is not None:
        messages.append({
            "role": "user",
            "content": f"<previous_summary>\n{prev_summary.content}\n</previous_summary>",
        })
    for m in raw_prefix:
        p: dict[str, Any] | None = m.to_openai()
        if p is not None:
            messages.append(p)
    # 固定 user 指令收尾，避免以 assistant 结尾触发续写
    messages.append({"role": "user", "content": SUMMARY_FINAL_INSTRUCTION})
    return messages


class Compactor:
    """会话历史压缩器：判断是否触发压缩并生成/落盘摘要。"""

    def __init__(self, provider: Provider, runtime_config: AgentRuntimeConfig):
        self.provider = provider
        self.runtime_config = runtime_config

    async def maybe_compress(self, context: Context) -> bool:
        """检查是否需要压缩，需要则生成摘要并落盘。返回是否发生了压缩。"""
        rc = self.runtime_config
        if not rc.context_compress_enabled:
            return False
        session_id, channel = context.session_id, context.channel
        raw = SessionStore.load_history(session_id, channel, include_summary=False)
        if not raw:
            return False
        rec = SessionStore.load_summary(channel, session_id)
        # 从 raw + rec 合成估算（与 context.messages 同口径），只读一次存储
        tools = context.tools
        total = estimate_session_payload(context.system_prompt, tools, raw, rec)
        window = rc.context_window_tokens
        trigger = min(
            window * rc.context_compress_trigger_ratio,
            window - rc.llm_config.max_tokens - 4096,
        )
        if total < trigger:
            return False
        tail_start = find_tail_boundary(raw, int(window * rc.context_compress_tail_ratio))
        if tail_start <= 0:
            return False
        # 压缩后 payload 预估（保守上界）：system + tools + 摘要上限 + 保留
        # 尾部。若仍不低于触发阈值，说明窗口装不下「摘要 + 尾部」，压缩无
        # 收益；放行会导致同一 turn 内每次迭代都重复压缩，故放弃。
        post = (estimate_session_payload(context.system_prompt, tools, [], None)
                + rc.context_compress_summary_max_tokens
                + sum(estimate_message(m) for m in raw[tail_start:]))
        if post >= trigger:
            logger.warning(
                "压缩后预估 %d tokens 仍不低于触发阈值 %.0f，压缩无收益，跳过"
                "（建议调大 context_window_tokens 或调小 max_tokens）: session=%s",
                post, trigger, session_id)
            return False
        summary = await self._call_summarize(raw[:tail_start], rec)
        if not summary:
            logger.warning("压缩模型返回空摘要，跳过本轮压缩: session=%s", session_id)
            return False
        # 残句/跑偏防御：截断的半截摘要（信息量为零）落盘会覆盖旧的好
        # 摘要，必须先过小节完整性校验
        if not self._validate_summary(summary):
            logger.warning(
                "摘要缺少必需小节（疑似被截断），放弃落盘保留旧摘要: session=%s",
                session_id)
            return False
        content = f"<summary>\n{summary}\n</summary>"
        try:
            SessionStore.save_summary(
                channel, session_id,
                SummaryRecord(content=content, covered_upto=raw[tail_start - 1].id),
            )
        except OSError:
            logger.exception("摘要落盘失败，跳过本轮压缩: session=%s", session_id)
            return False
        logger.info(
            "会话压缩完成: session=%s 覆盖 %d 条消息(至 %s)",
            session_id, tail_start, raw[tail_start - 1].id,
        )
        return True

    async def _call_summarize(
            self,
            raw_prefix: list[Message],
            prev_summary: SummaryRecord | None,
    ) -> str | None:
        """调用压缩模型生成摘要文本；任何异常/截断返回 None（fail-safe）。"""
        # reasoning 型模型的思考 tokens 计入 max_tokens 预算，会挤占正文
        # 空间导致输出被截断，故额外加一段余量
        budget = (self.runtime_config.context_compress_summary_max_tokens
                  + REASONING_TOKEN_HEADROOM)
        try:
            payload = LLMPayload(
                messages=build_summary_messages(raw_prefix, prev_summary),
                tools=None,
                llm_config=LLMConfig(
                    model=self.runtime_config.llm_config.model,
                    temperature=0,
                    max_tokens=budget,
                ),
            )
            parts: list[str] = []
            completion_tokens = 0
            async for kind, data in self.provider.stream_chunks(payload):
                if kind == "content":
                    parts.append(data)
                elif kind == "usage" and data:
                    if isinstance(data, dict):
                        completion_tokens = data.get("completion_tokens") or 0
                    else:
                        completion_tokens = getattr(data, "completion_tokens", 0) or 0
            if completion_tokens >= budget:
                logger.warning(
                    "压缩输出达到 max_tokens 被截断（completion=%d），放弃本轮摘要",
                    completion_tokens)
                return None
            return "".join(parts).strip()
        except Exception:  # noqa: BLE001 - 压缩失败必须 fail-safe，跳过本轮
            logger.exception("压缩摘要生成失败，跳过本轮压缩")
            return None

    @staticmethod
    def _validate_summary(text: str) -> bool:
        """摘要完整性校验：必须包含 Goal 与 Progress 两个必需小节。

        压缩提示词要求固定小节标题；缺小节说明输出被截断或模型未进入
        摘要框架（如复述原文）。标题匹配同时接受英文与中文写法。
        """
        low = text.lower()
        has_goal = "goal" in low or "目标" in text
        has_progress = "progress" in low or "进度" in text
        return has_goal and has_progress
