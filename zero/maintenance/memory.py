"""长期记忆沉淀（memory）维护任务。

读取 24h 内活跃会话的 summary.json（SessionSummary）与当前
~/.zero/memory.md，交给 LLM 合并出新的记忆文件并全量重写。空输出防护、
.bak 备份、tmp+replace 原子写。
"""
import datetime as dt
import json
import logging

from zero.paths import current
from zero.models import AgentRuntimeConfig, MaintenanceConfig
from zero.providers.base import Provider
from zero.session.models import SessionSummary
from zero.session.store import SessionStore

from zero.maintenance.llm import complete_text

logger = logging.getLogger(__name__)

# 会话筛选窗口
MEMORY_WINDOW = dt.timedelta(hours=24)


MEMORY_PROMPT = """You are maintaining the assistant's long-term memory file (Markdown).

The existing memory and recent session summaries are provided. Merge the durable, valuable
information from the summaries into memory and output the COMPLETE new memory file alone.

Rules:
- Extract only durable knowledge: user preferences, working conventions, recurring patterns, project facts, hard-won lessons.
- Drop one-off task details, transient state, anything already implied, and secrets.
- Preserve the existing structure/headings when reasonable; reorganize only when merging requires it.
- Hard limit: keep the output under ~100 lines. When over budget, prune stale or superseded entries first.
- Write in the language the existing memory uses (or the summaries' primary language if memory is empty)."""

MEMORY_FINAL_INSTRUCTION = (
    "Output the complete updated memory file content only — no explanations, "
    "no markdown fences."
)


async def run_memory(
        provider: Provider,
        runtime: AgentRuntimeConfig,
        maintenance_cfg: MaintenanceConfig,
) -> None:
    """入口：把 24h 活跃会话的复盘提炼进长期记忆。"""
    since = dt.datetime.now() - MEMORY_WINDOW
    summaries: list[SessionSummary] = []
    for channel in SessionStore.list_channels():
        for meta in SessionStore.list_recent(channel, since):
            s = SessionStore.load_summary(channel, meta.session_id)
            if s is not None:
                summaries.append(s)
    if not summaries:
        return  # 无新复盘信息，无需更新

    memory_path = current().memory
    current = ""
    if memory_path.exists():
        current = memory_path.read_text(encoding="utf-8").strip()

    summaries_json = json.dumps(
        [s.model_dump(mode="json") for s in summaries], ensure_ascii=False, indent=2)
    messages = [
        {"role": "system", "content": MEMORY_PROMPT},
        {"role": "user",
         "content": f"<current_memory>\n{current or '(empty)'}\n</current_memory>"},
        {"role": "user",
         "content": f"<session_summaries>\n{summaries_json}\n</session_summaries>"},
        {"role": "user", "content": MEMORY_FINAL_INSTRUCTION},
    ]
    new_text = await complete_text(
        provider, runtime.llm_call_options.model,
        messages, maintenance_cfg.memory_max_tokens,
        runtime.reasoning_token_headroom, label="记忆沉淀",
    )
    if not new_text:
        logger.warning("记忆沉淀输出为空，保留原记忆")
        return

    # 备份旧文件后原子写入（tmp + replace）
    if memory_path.exists():
        memory_path.with_suffix(".md.bak").write_text(
            memory_path.read_text(encoding="utf-8"), encoding="utf-8")
    tmp = memory_path.with_suffix(".md.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(memory_path)
    logger.info("记忆沉淀完成：合并 %d 份会话复盘", len(summaries))
