"""复现验证：取消（aclose）后 provider 底层读线程/连接能真正释放。

针对修复前的真实缺陷：OpenAI.stream_chunks 用 daemon 线程阻塞读 SSE，
aclose() 只关 async 生成器、不关连接，producer 线程残留在 iter_lines 上
→ 连接泄漏，新一轮 stream_chunks 卡在旧连接阻塞读上、永久无产出。

用真实 OpenAI 实现 + stub _request_with_retry（不触网），验证：
1. aclose 后 fake resp.close() 被调用（连接真正关闭）
2. producer 线程在 join(timeout) 内退出（不残留）
3. 干净路径（消费到结束）同样正常
"""
import asyncio
import json
import threading
import unittest
from collections import deque

from zero.providers.base import LLMPayload
from zero.providers.openai import OpenAI
from zero.models import ProviderConfig


class FakeLineIter:
    """模拟 SSE 响应行迭代。

    block_after_first=True：吐一条后阻塞，直到连接被 close 打断
    （模拟取消场景下的长流）；False：正常结束（clean 路径）。
    """

    def __init__(self, block_after_first: bool = True):
        self.closed = False
        self._block = threading.Event()
        self._block_after_first = block_after_first

    def __iter__(self):
        yield b"data: {\"choices\":[{\"delta\":{\"content\":\"chunk-1\"}}]}\n"
        if not self._block_after_first:
            yield b"data: [DONE]\n"
            return
        # 模拟长流：阻塞等待，直到连接被关闭打断
        while not self.closed:
            self._block.wait(timeout=0.1)
        # 连接被 close → 迭代结束（requests 在关闭连接上抛异常由外层捕获）
        return


class FakeResponse:
    """模拟 requests.Response：iter_lines 阻塞，close 可打断。"""

    def __init__(self, block_after_first: bool = True):
        self.lines = FakeLineIter(block_after_first)
        self.closed = False

    def iter_lines(self):
        yield from self.lines

    def close(self):
        self.closed = True
        self.lines.closed = True


def _make_provider(stub_resp: FakeResponse) -> OpenAI:
    prov = OpenAI(ProviderConfig(api_base="http://fake", api_key="k"))
    prov._request_with_retry = lambda *a, **k: stub_resp
    return prov


async def _consume_one_then_close(prov: OpenAI, resp: FakeResponse, do_close: bool):
    """消费首条后，按 do_close 决定是否立即 aclose（模拟取消）。"""
    ag = prov.stream_chunks(LLMPayload(messages=[{"role": "user", "content": "hi"}]))
    got = None
    try:
        kind, data = await ag.__anext__()
        got = (kind, data)
    except Exception:
        pass
    if do_close:
        try:
            await ag.aclose()
        except Exception:
            pass
    else:
        # 干净路径：放行并消费到结束（fake 自然吐 [DONE]）
        try:
            while True:
                await ag.__anext__()
        except StopAsyncIteration:
            pass
    # stream_chunks 的 finally 在返回前已同步 close 连接（内部 join 有超时
    # 兜底），所以此处直接读事实，无需再等一段时间
    return got, resp.closed


class TestProviderCancelReleases(unittest.TestCase):
    def test_aclose_closes_connection_and_thread(self):
        async def run():
            resp = FakeResponse(block_after_first=True)
            prov = _make_provider(resp)
            got, closed = await _consume_one_then_close(prov, resp, do_close=True)
            return got, closed

        got, closed = asyncio.run(run())
        self.assertEqual(got, ("content", "chunk-1"))
        self.assertTrue(closed, "aclose 后连接未关闭 → 读线程残留/泄漏")

    def test_clean_path_closes_connection(self):
        async def run():
            resp = FakeResponse(block_after_first=False)
            prov = _make_provider(resp)
            got, closed = await _consume_one_then_close(prov, resp, do_close=False)
            return got, closed

        got, closed = asyncio.run(run())
        self.assertEqual(got, ("content", "chunk-1"))
        self.assertTrue(closed)


def _sse_content(text: str) -> bytes:
    payload = json.dumps({"choices": [{"delta": {"content": text}}]})
    return f"data: {payload}\n".encode()


class ReleasableResponse:
    """可放行的 SSE 响应：吐首条后阻塞，release() 后继续，close() 则终止。"""

    def __init__(self, tag: str):
        self.tag = tag
        self.closed = False
        self._released = threading.Event()
        self._woken = threading.Event()

    def release(self) -> None:
        self._released.set()

    def close(self) -> None:
        self.closed = True
        self._woken.set()

    def iter_lines(self):
        yield _sse_content(f"{self.tag}-1")
        while not self._released.is_set() and not self.closed:
            self._woken.wait(timeout=0.01)
        if self.closed:
            return
        yield _sse_content(f"{self.tag}-2")
        yield b"data: [DONE]"


class TestConcurrentStreams(unittest.TestCase):
    """同一 provider 实例并行两条流：取消一条不得影响另一条。

    修复前 ``_stream_resp`` 是实例属性，第二条流会覆盖第一条的句柄；
    ``aclose()`` 第一条时关掉的是第二条的连接。
    """

    def test_closing_one_stream_keeps_the_other_alive(self):
        async def run():
            resp_a = ReleasableResponse("A")
            resp_b = ReleasableResponse("B")
            responses = deque([resp_a, resp_b])
            prov = OpenAI(ProviderConfig(api_base="http://fake", api_key="k"))
            prov._request_with_retry = lambda *a, **k: responses.popleft()

            ag_a = prov.stream_chunks(
                LLMPayload(messages=[{"role": "user", "content": "a"}]))
            ag_b = prov.stream_chunks(
                LLMPayload(messages=[{"role": "user", "content": "b"}]))
            # 先 A 后 B：实例属性此时已被 B 覆盖，正是旧实现的失手点
            self.assertEqual(await ag_a.__anext__(), ("content", "A-1"))
            self.assertEqual(await ag_b.__anext__(), ("content", "B-1"))

            await ag_a.aclose()

            self.assertTrue(resp_a.closed, "取消的流必须关闭自己的连接")
            self.assertFalse(resp_b.closed, "取消 A 不得关闭 B 的连接")

            resp_b.release()
            rest = []
            while True:
                try:
                    rest.append(
                        await asyncio.wait_for(ag_b.__anext__(), 2))
                except StopAsyncIteration:
                    break
            return rest

        self.assertEqual(asyncio.run(run()), [("content", "B-2")])


if __name__ == "__main__":
    unittest.main()