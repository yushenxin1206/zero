"""构造期必填校验：Provider 的端点/密钥、Message 的会话外键。

这些字段以前带"看起来能跑"的默认值（空端点 + 空密钥、随机 session_id），
失败被推迟到第一次请求 401，或让漏传 session_id 的消息静默落进一个随机
新会话。现在都要求显式声明，并在构造时报出可直接照做的错误。
"""
import pytest

from zero.models import Message, MessageBody, MessageIn, ProviderConfig
from zero.providers.openai import OpenAI


def provider_error(**config) -> str:
    with pytest.raises(ValueError) as excinfo:
        OpenAI(ProviderConfig(**config), "deepseek")
    return str(excinfo.value)


def test_provider_rejects_empty_api_key():
    msg = provider_error(api_base="https://deepseek.local", api_key="")
    assert "providers.deepseek" in msg
    assert "apiKey" in msg


def test_provider_rejects_empty_api_base():
    msg = provider_error(api_base="", api_key="k")
    assert "providers.deepseek" in msg
    assert "apiBase" in msg


def test_provider_without_provider_key_names_placeholder():
    with pytest.raises(ValueError) as excinfo:
        OpenAI(ProviderConfig())
    msg = str(excinfo.value)
    assert "providers.<厂商>" in msg
    assert "apiBase" in msg and "apiKey" in msg


def test_message_requires_session_id():
    with pytest.raises(ValueError) as excinfo:
        Message(channel="cli", sender="user", recipient="assistant",
                body=MessageBody(content="hi"))
    # pydantic 的报错位置用 camelCase 别名，这里只确认字段被点名
    assert "sessionId" in str(excinfo.value)


def test_message_in_requires_session_id():
    with pytest.raises(ValueError) as excinfo:
        MessageIn(channel="cli", body=MessageBody(content="hi"))
    assert "sessionId" in str(excinfo.value)
