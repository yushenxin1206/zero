"""
Provider for OpenAI-style LLM
"""
import json
import asyncio

from providers.base import Provider, LLMPayload
from datacls import ProviderConfig


class OpenAI(Provider):

    async def _stream_conn(
            self,
            payload: LLMPayload,
    ):
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
                content = j["choices"][0]["delta"].get("content", "")
                reasoning_content = j["choices"][0]["delta"].get("reasoning_content", "")
                if content:
                    yield 'content', content
                if reasoning_content:
                    yield 'reasoning_content', reasoning_content

            except json.JSONDecodeError:
                continue

    async def stream_chat_with_llm(
            self,
            payload: LLMPayload,
            max_retries: int = 3,
    ):
        async for text_chunk in self._stream_conn(payload):
            print(text_chunk, end="", flush=True)

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



