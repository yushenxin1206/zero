"""
Base provider
"""

import requests

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class Payload:
    model: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None
    tool_choice: dict | str | None = None
    response_format: dict[str, Any] | None = None
    stop: list[str] | None = None
    stream: bool | None = None
    stream_options: dict[str, Any] | None = None
    temperature: float | int = 0.1
    max_tokens: int = 8192
    reasoning_effort: str | None = None
    extra_body: dict[str, Any] | None = None

    @property
    def payload_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": self.messages,
            "tools": self.tools,
            "tool_choice": self.tool_choice,
            "response_format": self.response_format or {"type": "text"},
            "stop": self.stop,
            "stream": self.stream,
            "stream_options": self.stream_options,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "reasoning_effort": self.reasoning_effort,
        } | (self.extra_body or {})


class Provider(ABC):

    def __init__(
            self,
            api_base: str,
            api_key: str,
    ):
        self.api_base = api_base
        self.api_key = api_key

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
    async def _stream_conn(
            self,
            payload: Payload,
    ):
        pass

    @abstractmethod
    async def stream_chat_with_llm(
            self,
            payload: Payload,
            max_retries: int = 3,
    ):
        pass

    @abstractmethod
    async def chat_with_llm(
            self,
            payload: Payload,
            max_retries: int = 3,
    ):
        pass
