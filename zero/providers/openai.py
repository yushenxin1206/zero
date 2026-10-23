"""
Provider for OpenAI-style LLM
"""
import json
import asyncio
import logging
import threading
from typing import Any

from zero.providers.base import Provider, LLMPayload

logger = logging.getLogger(__name__)

# stream_chunks 内部哨兵：stop_event 胜出（用户终止打断挂起的读取）
_STOPPED = object()


class OpenAI(Provider):

    def _stream_conn(self, payload: LLMPayload, cancel_event: threading.Event | None = None):
        """Blocking SSE reader, runs in a worker thread (see stream_chunks).

        cancel_event：置位时在下一条 SSE 行边界退出。阻塞读本身无法被
        事件打断，真正的打断靠 stream_chunks 侧调用 resp.close() 关闭
        连接（这里在 finally 中也兜底关闭）。
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        payload.llm_call_options.stream = True
        # 要求厂商在流末尾返回 usage chunk；不支持的厂商会忽略该参数
        stream_options = dict(payload.llm_call_options.stream_options or {})
        stream_options.setdefault("include_usage", True)
        payload.llm_call_options.stream_options = stream_options

        resp = self._request_with_retry(
            self.api_base, headers, payload.payload_dict, stream=True,
        )
        # 暴露给 stream_chunks 的取消路径：取消时从消费者线程 close 连接，
        # 打断本线程的阻塞读
        self._stream_resp = resp
        try:
            for line in resp.iter_lines():
                if cancel_event is not None and cancel_event.is_set():
                    break
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
                choices = j.get("choices") or []
                choice = choices[0] if choices else {}
                delta = choice.get("delta", {})
                content = delta.get("content")
                if content:
                    yield "content", content
                reasoning = delta.get("reasoning_content")
                if reasoning:
                    yield "reasoning", reasoning
                for tc in delta.get("tool_calls") or []:
                    yield "tool_calls", tc
                # finish_reason 出现在本轮收尾 chunk（"stop" / "length" /
                # "tool_calls" 等）。透出给 runner 判断是否被 max_tokens 截断，
                # 避免"思考吃满预算、正文被截空"被静默当正常终稿。
                finish_reason = choice.get("finish_reason")
                if finish_reason:
                    yield "finish", finish_reason
                # usage 出现在最后一个 chunk（choices 为空）
                if usage := j.get("usage"):
                    yield "usage", usage
        finally:
            self._stream_resp = None
            resp.close()

    def _close_stream_resp(self) -> None:
        """取消路径：关闭正在流式读取的响应连接以打断阻塞读线程。"""
        resp = getattr(self, "_stream_resp", None)
        if resp is not None:
            try:
                resp.close()
            except Exception:  # noqa: BLE001 - close 失败无妨，线程将自然退出
                logger.debug("close streaming response failed", exc_info=True)

    async def stream_chunks(
            self,
            payload: LLMPayload,
            stop_event: asyncio.Event | None = None,
    ):
        """Async wrapper around the blocking SSE reader.

        The requests-based iteration runs in a daemon thread and feeds an
        asyncio.Queue, so the event loop stays responsive for UI rendering.

        可取消：生成器被 aclose/异常退出时，finally 会通知 producer 停止、
        关闭底层响应连接并等待线程退出，避免取消后残留读线程/连接泄漏
        （否则新一轮 stream_chunks 会卡在旧连接的阻塞读上，永久无产出）。

        stop_event：与取下一条流项竞争。模型思考期可能长时间无任何
        chunk（queue.get() 一直挂起），仅靠消费者侧 chunk 边界检查的
        终止请求根本到不了检查点——turn 悬挂、串行 loop 卡死后续轮次。
        stop 胜出即结束迭代，由 runner 流结束后安全点抛 TurnCancelled。
        """
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        cancel_event = threading.Event()   # 通知 producer 停止阻塞读

        def producer():
            try:
                for item in self._stream_conn(payload, cancel_event):
                    asyncio.run_coroutine_threadsafe(queue.put(item), loop).result()
                asyncio.run_coroutine_threadsafe(queue.put(None), loop)
            except Exception as e:  # noqa: BLE001 - surface any failure to consumer
                logger.exception("Streaming request failed")
                asyncio.run_coroutine_threadsafe(queue.put(e), loop)

        thread = threading.Thread(target=producer, daemon=True)
        try:
            thread.start()
            while True:
                item = await self._next_item(queue, stop_event)
                if item is _STOPPED or item is None:
                    break
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            # 无论正常结束、取消（aclose）还是异常，都真正释放底层资源：
            # 通知读线程在行边界退出 + 关闭响应连接打断阻塞读 + 等待线程
            # （超时兜底，不无限阻塞）。否则取消后残留读线程/连接泄漏，
            # 新一轮 stream 会卡在旧连接的阻塞读上、永久无产出
            cancel_event.set()
            self._close_stream_resp()
            thread.join(timeout=0.5)

    @staticmethod
    async def _next_item(queue: asyncio.Queue,
                         stop_event: asyncio.Event | None) -> Any:
        """取下一条流项；stop_event 置位时返回 _STOPPED 打断挂起的等待。"""
        if stop_event is None:
            return await queue.get()
        get_task = asyncio.ensure_future(queue.get())
        stop_task = asyncio.ensure_future(stop_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {get_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED)
            if stop_task in done:
                get_task.cancel()
                return _STOPPED
            return get_task.result()
        finally:
            stop_task.cancel()

    async def chat_with_llm(
            self,
            payload: LLMPayload,
            max_retries: int = 3,
    ):

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        payload.llm_call_options.stream = False

        resp = self._request_with_retry(
            self.api_base, headers, payload.payload_dict, max_retries=max_retries,
        )
        return resp.json()


if __name__ == '__main__':
    pass
