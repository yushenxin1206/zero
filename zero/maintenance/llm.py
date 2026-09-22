"""maintenance 任务共享的 LLM 调用助手。

复用 Compactor._call_compact 的调用模式：LLMPayload + stream_chunks
收集 content；completion_tokens >= budget 视为截断（reasoning 型模型的
思考 tokens 与正文共享 completion 预算，额外留 4096 余量）。
"""
import logging

from zero.models import LLMConfig
from zero.providers.base import LLMPayload, Provider

logger = logging.getLogger(__name__)

# reasoning 思考 tokens 计入 max_tokens 预算，不留余量正文会被截断
REASONING_TOKEN_HEADROOM = 4096


async def complete_text(
        provider: Provider,
        model: str,
        messages: list[dict],
        max_tokens: int,
        label: str,
) -> str | None:
    """调用 LLM 生成纯文本输出；任何异常/截断返回 None（fail-safe）。"""
    budget = max_tokens + REASONING_TOKEN_HEADROOM
    try:
        payload = LLMPayload(
            messages=messages,
            tools=None,
            llm_config=LLMConfig(model=model, temperature=0, max_tokens=budget),
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
                "%s 输出达到 max_tokens 被截断（completion=%d），放弃本次结果",
                label, completion_tokens)
            return None
        return "".join(parts).strip()
    except Exception:  # noqa: BLE001 - 维护任务失败只跳过本次，不影响主流程
        logger.exception("%s LLM 调用失败", label)
        return None
