"""
Base provider
"""

import asyncio
import time
import requests

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

from pydantic import Field

from zero.models import Base, LLMCallOptions, ProviderConfig

# 429 未带 Retry-After 时的退避基数（秒），按 2^attempt 递增；上限防止厂商
# 给出超大值把一轮对话卡死。
_RETRY_BACKOFF_SECONDS = 1.0
_RETRY_AFTER_CAP_SECONDS = 30.0


def _retry_after_seconds(resp: requests.Response, attempt: int) -> float:
    """429 的等待时长：优先厂商的 Retry-After（秒），否则指数退避。"""
    try:
        seconds = float(resp.headers.get('Retry-After', ''))
    except ValueError:
        seconds = _RETRY_BACKOFF_SECONDS * (2 ** attempt)
    return min(max(seconds, 0.0), _RETRY_AFTER_CAP_SECONDS)


class LLMPayload(Base):
    """LLM request payload: the contract between core (builder) and providers (executor).

    内容（messages / tools）+ 调用选项（llm_call_options）合成一次请求。
    """
    messages: list[dict[str, Any]] | None = None
    tools: list[dict[str, Any]] | None = None
    llm_call_options: LLMCallOptions = Field(default_factory=LLMCallOptions)

    @property
    def payload_dict(self) -> dict[str, Any]:
        base_data = self.model_dump(exclude={'llm_call_options'})
        base_data.update(self.llm_call_options.options_dict or {})
        return base_data


class Provider(ABC):

    def __init__(self, provider_config: ProviderConfig):
        self.api_base = provider_config.api_base
        self.api_key = provider_config.api_key

    @staticmethod
    def _request_with_retry(
            url: str,
            headers: dict[str, str],
            json_data: dict[str, Any],
            max_retries: int = 3,
            timeout: int = 60,
            stream: bool = False,
    ) -> requests.Response:
        """发请求，只重试「再试一次可能成功」的失败。

        可重试：超时 / 连接失败（网络层抖动）、5xx（服务端临时故障）、
        429（厂商明确要求稍后重试，按 Retry-After 等待后重试——它不属于
        客户端配置错误，不能和其余 4xx 一起立刻放弃）。
        不可重试：其余 4xx（401/403/404/422…）说明密钥、权限、端点或参数
        就是错的，把同一请求再打三遍只会重复同一个错误并拖慢报错；URL
        语法错误 / 缺少 scheme 同理，立即抛。
        """
        last_exc: requests.exceptions.RequestException | None = None
        for attempt in range(max_retries):
            try:
                resp = requests.post(
                    url, headers=headers, json=json_data,
                    timeout=timeout, stream=stream,
                )
            except (requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError) as e:
                last_exc = e
                continue
            if resp.status_code == 429 and attempt < max_retries - 1:
                resp.close()
                time.sleep(_retry_after_seconds(resp, attempt))
                continue
            try:
                resp.raise_for_status()
            except requests.exceptions.HTTPError as e:
                resp.close()
                if resp.status_code < 500:
                    raise  # 4xx（429 的次数已用尽也走这里）：重试没有意义
                last_exc = e
                continue
            return resp
        raise last_exc  # type: ignore[misc]

    @abstractmethod
    def stream_chunks(
            self,
            payload: LLMPayload,
            stop_event: "asyncio.Event | None" = None,
    ) -> AsyncIterator[tuple[str, Any]]:
        """Yield streamed LLM output as (kind, data) tuples.

        kind:
          - "content":   data = text delta (str)
          - "reasoning": data = reasoning delta (str)
          - "tool_calls": data = streamed tool_call fragment (dict)
          - "usage":     data = usage dict from the provider (final chunk)

        stop_event：用户终止信号。实现必须在等待下一条流项时与它竞争
        （如 asyncio.wait），置位后及时结束迭代——模型思考期可能长时间
        无任何 chunk，若只依赖消费者侧的 chunk 边界检查，终止请求会一直
        悬挂在挂起的读取上，turn 永不结束。None（如子 agent 单次模式）
        表示无终止语义，行为与普通流一致。
        """
        pass

    @abstractmethod
    async def chat_with_llm(
            self,
            payload: LLMPayload,
            max_retries: int = 3,
    ):
        pass
