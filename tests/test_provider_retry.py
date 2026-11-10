"""Provider 请求重试策略：只重试「再试一次可能成功」的失败。

用假的 requests.post 驱动，不触网；断言实际请求次数与 429 的等待时长，
覆盖 4xx 立即失败、5xx / 超时 / 连接失败重试、429 按 Retry-After 重试。
"""
import requests
import pytest

from zero.providers import base as base_mod
from zero.providers.base import Provider


class FakeResponse:
    def __init__(self, status_code: int, headers: dict | None = None):
        self.status_code = status_code
        self.headers = headers or {}
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(
                f'{self.status_code} error', response=self)

    def close(self):
        self.closed = True


def install_post(monkeypatch, outcomes):
    """按顺序返回 outcomes（Exception 则抛出）；返回请求记录列表。"""
    calls: list[str] = []

    def post(url, **_kwargs):
        item = outcomes[len(calls)]
        calls.append(url)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(base_mod.requests, 'post', post)
    return calls


@pytest.mark.parametrize('status', [401, 403, 404, 422])
def test_client_error_is_not_retried(monkeypatch, status):
    resp = FakeResponse(status)
    calls = install_post(monkeypatch, [resp, FakeResponse(200)])
    with pytest.raises(requests.exceptions.HTTPError) as excinfo:
        Provider._request_with_retry('http://x', {}, {})
    assert excinfo.value.response.status_code == status
    assert len(calls) == 1, '客户端错误被重试了'
    assert resp.closed


def test_url_error_is_not_retried(monkeypatch):
    """配置类错误（apiBase 为空 → MissingSchema）不是网络抖动，不重试。"""
    calls = install_post(monkeypatch, [
        requests.exceptions.MissingSchema('no scheme'),
        FakeResponse(200),
    ])
    with pytest.raises(requests.exceptions.MissingSchema):
        Provider._request_with_retry('', {}, {})
    assert len(calls) == 1


def test_server_error_is_retried(monkeypatch):
    resps = [FakeResponse(500), FakeResponse(502), FakeResponse(503)]
    calls = install_post(monkeypatch, resps)
    with pytest.raises(requests.exceptions.HTTPError) as excinfo:
        Provider._request_with_retry('http://x', {}, {})
    assert excinfo.value.response.status_code == 503
    assert len(calls) == 3
    assert all(r.closed for r in resps)


def test_timeout_is_retried(monkeypatch):
    calls = install_post(
        monkeypatch, [requests.exceptions.Timeout('slow')] * 3)
    with pytest.raises(requests.exceptions.Timeout):
        Provider._request_with_retry('http://x', {}, {})
    assert len(calls) == 3


def test_connection_error_is_retried(monkeypatch):
    calls = install_post(
        monkeypatch, [requests.exceptions.ConnectionError('reset')] * 3)
    with pytest.raises(requests.exceptions.ConnectionError):
        Provider._request_with_retry('http://x', {}, {})
    assert len(calls) == 3


def test_rate_limit_waits_retry_after_then_succeeds(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(base_mod.time, 'sleep', sleeps.append)
    limited = FakeResponse(429, {'Retry-After': '2'})
    ok = FakeResponse(200)
    calls = install_post(monkeypatch, [limited, ok])

    assert Provider._request_with_retry('http://x', {}, {}) is ok
    assert len(calls) == 2
    assert sleeps == [2.0]
    assert limited.closed


def test_rate_limit_without_header_backs_off(monkeypatch):
    """厂商不给 Retry-After 时按指数退避（第 0 次 1s，第 1 次 2s）。"""
    sleeps: list[float] = []
    monkeypatch.setattr(base_mod.time, 'sleep', sleeps.append)
    calls = install_post(monkeypatch, [
        FakeResponse(429), FakeResponse(429), FakeResponse(200)])
    Provider._request_with_retry('http://x', {}, {})
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]


def test_rate_limit_exhausted_raises(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(base_mod.time, 'sleep', sleeps.append)
    resps = [FakeResponse(429, {'Retry-After': '0'}) for _ in range(3)]
    calls = install_post(monkeypatch, resps + [FakeResponse(200)])
    with pytest.raises(requests.exceptions.HTTPError) as excinfo:
        Provider._request_with_retry('http://x', {}, {})
    assert excinfo.value.response.status_code == 429
    assert len(calls) == 3
    assert sleeps == [0.0, 0.0], '最后一次尝试前不应再等待'
