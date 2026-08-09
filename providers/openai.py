"""
Provider for OpenAI-style LLM
"""
import requests
import json
import asyncio
from typing import Any

from providers.base import Provider


class OpenAI(Provider):

    def generate_payload(
            self,
            model: str,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None = None,
            tool_choice: dict | str | None = None,
            response_format: dict[str, Any] | None = None,
            stop: list[str] | None = None,
            stream: bool = False,
            stream_options: dict[str, Any] | None = None,
            temperature: float | int = 0.1,
            max_tokens: int = 8192,
            reasoning_effort: str | None = None,
            extra_body: dict[str, Any] | None = None,
    ):
        return  {
            "model": model,
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
            "response_format": response_format or {"type": "text"},
            "stop": stop,
            "stream": stream,
            "stream_options": stream_options,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "reasoning_effort": reasoning_effort,
        } | (extra_body or {})

    async def _stream_conn(
            self,
            payload: dict[str, Any],
    ):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        payload["stream"] = True

        resp = requests.post(
            self.api_base, headers=headers, json=payload, stream=True, timeout=60
        )
        resp.raise_for_status()  # http错误抛出异常

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
                reasoning_content =  j["choices"][0]["delta"].get("reasoning_content", "")
                if content:
                    yield 'content', content
                if reasoning_content:
                    yield 'reasoning_content', reasoning_content

            except json.JSONDecodeError:
                continue

    async def stream_chat_with_llm(
            self,
            payload: dict[str, Any],
            max_retries: int = 3,
    ):
        async for text_chunk in self._stream_conn(payload):
            print(text_chunk, end="", flush=True)


    async def chat_with_llm(
            self,
            payload: dict[str, Any],
            max_retries: int = 3,
    ):

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        payload["stream"] = False

        resp = requests.post(self.api_base, headers=headers, json=payload, stream=False, timeout=60)

        resp.raise_for_status()

        return resp.json()


if __name__ == '__main__':
    openai = OpenAI(
        api_key='sk-d72f4b4051084c4bbb185b73b37051b5',
        api_base='https://api.deepseek.com/chat/completions',
    )

    payload = openai.generate_payload(
        model='deepseek-v4-flash',
        messages=[
            {
                "content": "You are a helpful assistant",
                "role": "system"
            },
            {
                "content": "Hi",
                "role": "user"
            }
        ],
        extra_body={
            "thinking": {
                "type": "enabled"
            }
        },
        reasoning_effort="low",
    )

    resp = asyncio.run(
        openai.chat_with_llm(
            payload=payload
        )
    )

    print(resp)