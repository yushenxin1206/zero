"""构造期必填校验：Message 的会话外键。

session_id 以前带 uuid4 默认值，漏传的消息会静默落进一个随机新会话，
而不是在构造时报错。现在要求显式声明。
"""
import pytest

from zero.models import Message, MessageBody, MessageIn


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
