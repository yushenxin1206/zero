"""Web 历史消息要带上 token 用量，供聊天气泡下方展示。"""

from zero.channels.web.server import WebChannel
from zero.models import MessageBody, MessageOut, TokenUsage


class FakeService:
    def __init__(self, messages):
        self._messages = messages

    def display_history(self, session_id):
        return self._messages

    def approvals(self, session_id):
        return []


def test_history_serializes_token_usage_as_camel_case():
    message = MessageOut(
        channel="web", session_id="s1", turn_id="t1",
        body=MessageBody(
            content="回答",
            token_usage=TokenUsage(
                prompt_tokens=11, completion_tokens=22, total_tokens=33),
        ),
    )
    channel = object.__new__(WebChannel)
    channel.service = FakeService([message])

    payload = channel._history_sync("s1")

    usage = payload["messages"][0]["usage"]
    assert usage["promptTokens"] == 11
    assert usage["completionTokens"] == 22
    assert usage["totalTokens"] == 33


def test_history_without_usage_stays_clean():
    message = MessageOut(
        channel="web", session_id="s1", turn_id="t1",
        body=MessageBody(content="回答"),
    )
    channel = object.__new__(WebChannel)
    channel.service = FakeService([message])

    payload = channel._history_sync("s1")

    assert "usage" not in payload["messages"][0]
