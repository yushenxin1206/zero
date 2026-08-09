"""
Base provider
"""
import requests
import json

from abc import ABC, abstractmethod
from typing import Any


class Provider(ABC):

    def __init__(
            self,
            api_base: str,
            api_key: str,
    ):
        self.api_base = api_base
        self.api_key = api_key

    @abstractmethod
    def generate_payload(
            self,
            model: str,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None = None,
            tool_choice: dict | str | None = None,
            response_format: dict[str, Any] | None = None,
            stop: list[str] | None = None,
            stream: bool | None = None,
            stream_options: dict[str, Any] | None = None,
            temperature: float | int = 0.1,
            max_tokens: int = 8192,
            reasoning_effort: str | None = None,
            extra_body: dict[str, Any] | None = None,
    ):
        pass

    @abstractmethod
    async def _stream_conn(
            self,
            payload: dict[str, Any],
    ):
       pass

    @abstractmethod
    async def stream_chat_with_llm(
            self,
            payload: dict[str, Any],
            max_retries: int = 3,
    ):
        pass


    @abstractmethod
    async def chat_with_llm(
            self,
            payload: dict[str, Any],
            max_retries: int = 3,
    ):
        pass



