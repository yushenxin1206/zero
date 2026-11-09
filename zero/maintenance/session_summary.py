"""会话复盘（session_summary）维护任务。

筛选各渠道 index.json 中 last_updated_at 在 24 小时内的会话，逐会话调
LLM 生成结构化复盘，写入会话级 summary.json（SessionSummary），并把
title / description 回写到渠道索引。增量锚点 covered_upto 让重复运行
只把新消息送入 LLM，无新消息则跳过（幂等省 token）。
"""
import asyncio
import datetime as dt
import json
import logging

from zero.models import AgentRuntimeConfig, MaintenanceConfig, Message
from zero.providers.base import Provider
from zero.session.models import SessionSummary
from zero.session.store import SessionStore

from zero.maintenance.llm import complete_text

logger = logging.getLogger(__name__)

# 会话筛选窗口
SESSION_SUMMARY_WINDOW = dt.timedelta(hours=24)

SESSION_SUMMARY_PROMPT = """You are reviewing a conversation session to produce a structured retrospective summary.

Output ONLY a valid JSON object — no markdown fences, no text outside the JSON — with exactly these fields:

{
  "title": "short session title, max ~30 chars",
  "description": "one-paragraph summary of the session, 2-3 sentences",
  "done": ["what got done — concise bullet points"],
  "went_well": ["what worked well"],
  "went_wrong": ["what went poorly"],
  "improvements": ["how to do better next time"]
}

Rules:
- A <previous_summary> block, if present, summarizes earlier messages of this session: merge it, keep still-valid facts, update or drop stale ones.
- Record outcomes and state, not step-by-step transcripts. Keep exact wording only for paths, commands, error text, and names.
- Each list must contain at least one item; use "(none)" as the single item when nothing applies.
- Write all values in the conversation's primary language."""

SESSION_SUMMARY_FINAL_INSTRUCTION = (
    "Review the conversation above (merging the <previous_summary> block if "
    "present) and output the JSON object with exactly the required fields. "
    "Output only the JSON — do not continue the conversation."
)


async def run_session_summary(
        provider: Provider,
        runtime: AgentRuntimeConfig,
        maintenance_cfg: MaintenanceConfig,
) -> None:
    """入口：复盘所有渠道 24h 内活跃的会话。单会话失败不中断批次。"""
    since = dt.datetime.now() - SESSION_SUMMARY_WINDOW
    # 索引扫描是阻塞 IO：先在线程池筛出目标会话，再逐个异步复盘
    targets = await asyncio.to_thread(_collect_targets, since)
    for channel, session_id in targets:
        try:
            await _summarize_session(
                provider, runtime, maintenance_cfg, channel, session_id)
        except Exception:  # noqa: BLE001 - 单会话失败不中断批次
            logger.exception(
                "会话复盘失败: channel=%s session=%s", channel, session_id)


def _collect_targets(since: dt.datetime) -> list[tuple[str, str]]:
    """窗口内活跃会话的 (channel, session_id) 列表（阻塞 IO，由线程池调用）。"""
    return [
        (channel, meta.session_id)
        for channel in SessionStore.list_channels()
        for meta in SessionStore.list_recent(channel, since)
    ]


def _build_messages(prev: SessionSummary | None, tail: list[Message]) -> list[dict]:
    """构造复盘模型的输入消息（旧复盘块 + 增量尾部 + user 指令收尾）。"""
    messages: list[dict] = [{"role": "system", "content": SESSION_SUMMARY_PROMPT}]
    if prev is not None:
        prev_json = json.dumps(prev.model_dump(mode="json"), ensure_ascii=False, indent=2)
        messages.append({
            "role": "user",
            "content": f"<previous_summary>\n{prev_json}\n</previous_summary>",
        })
    for m in tail:
        p = m.to_openai()
        if p is not None:
            messages.append(p)
    # 固定 user 指令收尾，避免以 assistant 结尾触发续写
    messages.append({"role": "user", "content": SESSION_SUMMARY_FINAL_INSTRUCTION})
    return messages


def _extract_json(text: str) -> dict | None:
    """从 LLM 输出提取 JSON 对象；宽容剥离 ``` 围栏；失败返回 None。"""
    text = text.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        text = text[first_nl + 1:] if first_nl != -1 else ""
        stripped = text.rstrip()
        if stripped.endswith("```"):
            text = stripped[:-3]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


async def _summarize_session(
        provider: Provider,
        runtime: AgentRuntimeConfig,
        maintenance_cfg: MaintenanceConfig,
        channel: str,
        session_id: str,
) -> None:
    """复盘单个会话：生成 SessionSummary 并落盘，回写索引。"""
    # 含压缩合成消息（load_history 默认 include_compact=True），天然有界；
    # 历史/复盘读取放线程池，避免阻塞事件循环
    raw = await asyncio.to_thread(
        SessionStore.load_history, session_id, channel)
    if not raw:
        return
    prev = await asyncio.to_thread(
        SessionStore.load_summary, channel, session_id)
    tail = raw
    hit = False
    if prev is not None:
        for i, m in enumerate(raw):
            if m.id == prev.covered_upto:
                hit = True
                tail = raw[i + 1:]
                break
        # 锚点未命中（如被后续压缩覆盖）→ tail 保持全量合成视图
    if hit and not tail:
        return  # 上次复盘后无新消息，幂等跳过

    text = await complete_text(
        provider, runtime.llm_call_options.model,
        _build_messages(prev, tail),
        maintenance_cfg.session_summary_max_tokens,
        runtime.reasoning_token_headroom,
        label=f"会话复盘({session_id[:8]})",
    )
    if not text:
        return
    data = _extract_json(text)
    if data is None:
        logger.warning("会话复盘输出不是有效 JSON，跳过: session=%s", session_id)
        return
    try:
        summary = SessionSummary.model_validate(data)
    except ValueError:
        logger.warning("会话复盘 JSON 不符合 SessionSummary 结构，跳过: session=%s", session_id)
        return
    summary.covered_upto = raw[-1].id
    summary.updated_at = dt.datetime.now()
    await asyncio.to_thread(
        SessionStore.save_summary, channel, session_id, summary)
    # 回写索引（update_meta_by_summary 绝不触碰 last_updated_at，防 24h 过滤自激）
    await asyncio.to_thread(
        SessionStore.update_meta_by_summary,
        channel, session_id,
        title=summary.title or None,
        description=summary.description or None,
    )
    logger.info("会话复盘完成: channel=%s session=%s", channel, session_id)
