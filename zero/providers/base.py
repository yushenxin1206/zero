"""
Base provider
"""

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

    def __init__(
            self,
            provider_config: ProviderConfig,
    ):
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
    ) -> AsyncIterator[tuple[str, Any]]:
        """Yield streamed LLM output as (kind, data) tuples.

        kind:
          - "content":   data = text delta (str)
          - "reasoning": data = reasoning delta (str)
          - "tool_calls": data = streamed tool_call fragment (dict)
          - "usage":     data = usage dict from the provider (final chunk)
        """
        pass

    @abstractmethod
    async def chat_with_llm(
            self,
            payload: LLMPayload,
            max_retries: int = 3,
    ):
        pass
