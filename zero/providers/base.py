"""
Base provider
"""

import asyncio
import requests

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

from pydantic import Field

from zero.models import Base, LLMCallOptions, ProviderConfig


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
        last_exc: requests.exceptions.RequestException | None = None
        for _ in range(max_retries):
            try:
                resp = requests.post(
                    url, headers=headers, json=json_data,
                    timeout=timeout, stream=stream,
                )
                resp.raise_for_status()
                return resp
            except requests.exceptions.RequestException as e:
                last_exc = e
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
