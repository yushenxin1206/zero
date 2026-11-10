"""
会话上下文自动压缩。

每次构建 LLM payload 前调用 Compactor.maybe_compress：当历史估算超出触发
阈值时，把历史前缀交给压缩模型生成压缩记录并落盘 compact.json；后续
context.messages / load_history 用 [压缩消息] + 尾部 合成 payload。

设计约束：
- 纯核心实现，不经过 hooks（hook 无法替换已构建的 payload）。
- messages.jsonl 保持 append-only；压缩记录独立存储，删除 compact.json 即可
  恢复全量历史。
- 压缩失败必须 fail-safe：跳过本轮，继续使用完整历史，只记日志。
"""
import asyncio
import json
import logging
import math
import unicodedata
from typing import Any

from zero.core.context import Context
from zero.models import (
    AgentRuntimeConfig,
    CompactConfig,
    CompactRecord,
    LLMCallOptions,
    Message,
)
from zero.providers.base import LLMPayload, Provider
from zero.session.store import SessionStore

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
        rec: CompactRecord | None,
) -> int:
    """估算当前会话待发 payload 的 token 占用。

    与 estimate_payload(context.messages, context.tools) 口径一致，但直接
    从 turn 级快照的全量历史 raw 与压缩记录 rec 合成（[压缩] + 尾部；找不到
    covered_upto 时退化为全量，不计压缩记录）。
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
- A <previous_compact> block, if present, is the sole record of earlier history: merge all its facts, prune what is stale or superseded.
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

# 压缩输出预算中的 reasoning 余量不在此硬编码：由 compact 任务解析结果
# 的 reasoning_token_headroom 提供（见 _call_compact），主对话触发阈值里的
# 余量取主对话 runtime 的 reasoning_token_headroom（见 maybe_compress）。

# 以 user 指令收尾：防止历史以 assistant 结尾时，模型把压缩任务误当成
# "续写对话"（把原回复原文当摘要输出的已知失效模式）
COMPACT_FINAL_INSTRUCTION = (
    "Summarize the conversation above (merging the <previous_compact> block if "
    "present) following the required section headings. Output only the summary — "
    "do not continue the conversation or copy passages verbatim."
)


def build_compact_messages(
        raw_prefix: list[Message],
        prev_compact: CompactRecord | None,
) -> list[dict]:
    """把待压缩的历史前缀转成压缩模型的输入消息。"""
    messages: list[dict] = [
        {"role": "system", "content": COMPRESS_PROMPT}
    ]
    if prev_compact is not None:
        messages.append({
            "role": "user",
            "content": f"<previous_compact>\n{prev_compact.content}\n</previous_compact>",
        })
    for m in raw_prefix:
        p: dict[str, Any] | None = m.to_openai()
        if p is not None:
            messages.append(p)
    # 固定 user 指令收尾，避免以 assistant 结尾触发续写
    messages.append({"role": "user", "content": COMPACT_FINAL_INSTRUCTION})
    return messages


class Compactor:
    """会话历史压缩器：判断是否触发压缩并生成/落盘压缩记录。

    三份输入分工：
    - session_runtime：主对话运行时（判断的正是主对话 payload 是否超窗）——
      取窗口、主对话输出预算、主对话 reasoning 余量；
    - compact_runtime：compact 任务解析结果（config.resolve_runtime("compact")）
      ——取压缩调用的模型/temperature 及该任务的 reasoning 余量；
    - compact_config：顶层 CompactConfig（config.compact）——压缩功能的
      开关/触发比例/尾部比例/压缩记录输出上限。
    """

    def __init__(self, provider: Provider, session_runtime: AgentRuntimeConfig,
                 compact_runtime: AgentRuntimeConfig,
                 compact_config: CompactConfig, *,
                 session_store: SessionStore):
        self.provider = provider
        self.session_runtime = session_runtime
        # 压缩记录落盘走的会话存储，由上层显式注入
        self.session_store = session_store
        # 压缩调用的参数取自 compact 任务解析结果（模型/temperature 可与
        # 主对话不同）；调用方必须 resolve_runtime("compact") 传入
        self.compact_runtime = compact_runtime
        self.compact_llm_options = compact_runtime.llm_call_options
        # 压缩功能策略（开关/比例/输出上限）取自顶层 compact 区块
        self.compact_config = compact_config

    async def maybe_compress(self, context: Context) -> bool:
        """检查是否需要压缩，需要则生成压缩记录并落盘。返回是否发生了压缩。"""
        if not self.compact_config.enabled:
            return False
        raw = context.raw_history
        if not raw:
            return False
        record = await self.compress_for_target(
            session_id=context.session_id,
            channel=context.channel,
            raw=raw,
            rec=context.compact_record,
            system_prompt=context.system_prompt,
            tools=context.tools,
            target=self.session_runtime,
        )
        if record is not None:
            # 回写 turn 级快照：后续迭代的 messages 立即合成 [压缩] + 尾部
            context.compact_record = record
            return True
        return False

    async def compress_for_target(
            self,
            *,
            session_id: str,
            channel: str,
            raw: list[Message],
            rec: CompactRecord | None,
            system_prompt: str,
            tools: list[dict] | None,
            target: AgentRuntimeConfig,
            force: bool = False,
            provider: Provider | None = None,
            llm_options: LLMCallOptions | None = None,
    ) -> CompactRecord | None:
        """针对 target 的窗口/预算执行一次压缩，产出则落盘并返回记录。

        与 maybe_compress 的区别在参数化：target 决定触发阈值/尾部/输出预算
        的参照窗口（主对话自动压缩传 session_runtime；会话切换模型时传新
        模型的目标运行时）。provider / llm_options 缺省取 compact 任务自身
        （即正常压缩路径），切换时由调用方传「旧模型」以规避新窗口装不下
        前缀导致的溢出。force=True 时压缩后预估仍超阈值也尽力落盘（切换
        场景），否则与正常路径一致地视为无收益跳过。
        """
        provider = provider or self.provider
        llm_options = llm_options or self.compact_llm_options
        # 从 raw + rec 合成估算，与 context.messages 同口径
        total = estimate_session_payload(system_prompt, tools, raw, rec)
        window = target.llm_runtime.context_window_tokens
        trigger = min(
            window * self.compact_config.trigger_ratio,
            window - target.llm_call_options.max_tokens - target.reasoning_token_headroom,
        )
        if total < trigger:
            return None
        tail_start = find_tail_boundary(raw, int(window * self.compact_config.tail_ratio))
        if tail_start <= 0:
            return None
        # 压缩后 payload 预估（保守上界）：system + tools + 压缩记录上限 + 保留
        # 尾部。若仍不低于触发阈值，说明窗口装不下「压缩记录 + 尾部」，压缩
        # 无收益；正常路径放行会导致同 turn 内每次迭代都重复压缩，故放弃。
        post = (estimate_session_payload(system_prompt, tools, [], None)
                + self.compact_config.context_compact_max_tokens
                + sum(estimate_message(m) for m in raw[tail_start:]))
        if post >= trigger and not force:
            logger.warning(
                "压缩后预估 %d tokens 仍不低于触发阈值 %.0f，压缩无收益，跳过"
                "（建议调大 context_window_tokens 或调小 max_tokens）: session=%s",
                post, trigger, session_id)
            return None
        # 增量压缩：prev_compact 已承载 covered_upto 之前的全部历史，只送其后的
        # 新消息，避免每次重送全量前缀导致压缩输入随历史无界增长（第 N 次链式
        # 压缩超过压缩模型窗口 → API 报错 → fail-safe 跳过 → 压缩永久停滞）。
        # 与 apply_compact / estimate_session_payload 的 fallback 语义一致：
        # covered_upto 定位失败时退化为全量，并丢弃旧记录（否则与全量前缀重复）。
        prefix_start = 0
        prev = rec
        if rec is not None:
            for i, m in enumerate(raw):
                if m.id == rec.covered_upto:
                    prefix_start = i + 1
                    break
            else:
                prev = None
        if prefix_start >= tail_start:
            return None  # 无未覆盖消息可压缩（如窗口调小导致边界回退）
        text = await self._call_compact(
            provider, llm_options, raw[prefix_start:tail_start], prev)
        if not text:
            logger.warning("压缩模型返回空结果，跳过本轮压缩: session=%s", session_id)
            return None
        # 残句/跑偏防御：截断的半截压缩记录（信息量为零）落盘会覆盖旧的
        # 好记录，必须先过小节完整性校验
        if not self._validate_compact(text):
            logger.warning(
                "压缩记录缺少必需小节（疑似被截断），放弃落盘保留旧记录: session=%s",
                session_id)
            return None
        content = f"<compact>\n{text}\n</compact>"
        record = CompactRecord(
            content=content, covered_upto=raw[tail_start - 1].id)
        try:
            await asyncio.to_thread(
                self.session_store.save_compact, channel, session_id, record)
        except OSError:
            logger.warning(
                "压缩记录落盘失败，跳过本轮压缩: session=%s",
                session_id, exc_info=True)
            return None
        logger.info(
            "会话压缩完成: session=%s 覆盖 %d 条消息(至 %s)",
            session_id, tail_start, raw[tail_start - 1].id,
        )
        return record

    async def _call_compact(
            self,
            provider: Provider,
            llm_options: LLMCallOptions,
            raw_prefix: list[Message],
            prev_compact: CompactRecord | None,
    ) -> str | None:
        """调用压缩模型生成压缩记录文本；任何异常/截断返回 None（fail-safe）。

        输出预算 = 顶层 CompactConfig 的 context_compact_max_tokens（压缩
        记录上限，不再读 compact 任务的 llm_call_options.max_tokens）+
        compact 任务解析结果的 reasoning_token_headroom：思考 tokens 与正文
        共享同一 completion 预算，不留余量时正文会被截断成残句。temperature
        等其余参数同样由 llm_options 决定（正常路径取 compact 任务，切换
        路径取旧模型）。
        """
        budget = (self.compact_config.context_compact_max_tokens
                  + self.compact_runtime.reasoning_token_headroom)
        try:
            payload = LLMPayload(
                messages=build_compact_messages(raw_prefix, prev_compact),
                tools=None,
                llm_call_options=llm_options.model_copy(
                    update={'max_tokens': budget}),
            )
            parts: list[str] = []
            completion_tokens = 0
            async for kind, data in provider.stream_chunks(payload):
                if kind == "content":
                    parts.append(data)
                elif kind == "usage" and data:
                    if isinstance(data, dict):
                        completion_tokens = data.get("completion_tokens") or 0
                    else:
                        completion_tokens = getattr(data, "completion_tokens", 0) or 0
            if completion_tokens >= budget:
                logger.warning(
                    "压缩输出达到 max_tokens 被截断（completion=%d），放弃本轮压缩",
                    completion_tokens)
                return None
            return "".join(parts).strip()
        except Exception:  # noqa: BLE001 - 压缩失败必须 fail-safe，跳过本轮
            logger.warning("压缩记录生成失败，跳过本轮压缩", exc_info=True)
            return None

    @staticmethod
    def _validate_compact(text: str) -> bool:
        """压缩记录完整性校验：必须包含 Goal 与 Progress 两个必需小节。

        压缩提示词要求固定小节标题；缺小节说明输出被截断或模型未进入
        压缩框架（如复述原文）。标题匹配同时接受英文与中文写法。
        """
        low = text.lower()
        has_goal = "goal" in low or "目标" in text
        has_progress = "progress" in low or "进度" in text
        return has_goal and has_progress
