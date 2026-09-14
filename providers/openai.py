"""
Provider for OpenAI-style LLM
"""
import json
import asyncio
import logging
import threading
from typing import Any, Iterator

from providers.base import Provider, LLMPayload
from datacls import ProviderConfig

logger = logging.getLogger(__name__)


class OpenAI(Provider):

    def _stream_conn(self, payload: LLMPayload) -> Iterator[tuple[str, Any]]:
        """Blocking SSE reader, runs in a worker thread (see stream_chunks)."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        payload.llm_config.stream = True

        resp = self._request_with_retry(
            self.api_base, headers, payload.payload_dict, stream=True,
        )

        for line in resp.iter_lines():
            if not line:
                continue
            s = line.decode("utf-8")
            if not s.startswith("data: "):
                continue
            raw = s[6:]
            if raw == "[DONE]":
                break
            try:
                j = json.loads(raw)
            except json.JSONDecodeError:
                continue
            delta = j.get("choices", [{}])[0].get("delta", {})
            content = delta.get("content")
            if content:
                yield "content", content
            reasoning = delta.get("reasoning_content")
            if reasoning:
                yield "reasoning", reasoning
            for tc in delta.get("tool_calls") or []:
                yield "tool_calls", tc

    async def stream_chunks(self, payload: LLMPayload):
        """Async wrapper around the blocking SSE reader.

        The requests-based iteration runs in a daemon thread and feeds an
        asyncio.Queue, so the event loop stays responsive for UI rendering.
        """
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def producer():
            try:
                for item in self._stream_conn(payload):
                    asyncio.run_coroutine_threadsafe(queue.put(item), loop).result()
                asyncio.run_coroutine_threadsafe(queue.put(None), loop)
            except Exception as e:  # noqa: BLE001 - surface any failure to consumer
                logger.exception("Streaming request failed")
                asyncio.run_coroutine_threadsafe(queue.put(e), loop)

        threading.Thread(target=producer, daemon=True).start()

        while True:
            item = await queue.get()
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            yield item

    async def chat_with_llm(
            self,
            payload: LLMPayload,
            max_retries: int = 3,
    ):

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        payload.llm_config.stream = False

        resp = self._request_with_retry(
            self.api_base, headers, payload.payload_dict, max_retries=max_retries,
        )
        return resp.json()


if __name__ == '__main__':
    pass
