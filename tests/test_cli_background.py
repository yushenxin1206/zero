"""后台 turn 结果记录：审批协议解析与命令展示的回归测试。"""
import asyncio

from zero.channels.cli.background import CliBackground
from zero.models import ApprovalOption, ApprovalRequest, StreamEvent


def make_background() -> CliBackground:
    return CliBackground(
        renderer=None, input_=None, render_lock=asyncio.Lock(),
        stream_enabled=lambda: False)


def approval_json(summary: str = "rm -rf /tmp/x") -> str:
    return ApprovalRequest(
        req_id="r1", title="shell 命令审批", summary=summary,
        options=[ApprovalOption(key="y", label="允许一次")],
    ).model_dump_json()


def test_approval_request_parses_protocol():
    ev = StreamEvent(kind="approval", content=approval_json())
    req = ev.approval_request()
    assert req is not None and req.summary == "rm -rf /tmp/x"


def test_approval_request_none_for_other_kinds_and_bad_payload():
    assert StreamEvent(kind="delta", content="x").approval_request() is None
    assert StreamEvent(
        kind="approval", content="{not json").approval_request() is None


def test_background_approval_notice_shows_command():
    """回归：此前读不存在的 command 键，提示里的命令永远为空。"""
    bg = make_background()
    bg.handle_event(StreamEvent(
        kind="approval", content=approval_json(), turn_id="t1",
        session_id="s1"))
    rec = bg._results.get_nowait()
    assert rec.kind == "approval_blocked"
    assert "rm -rf /tmp/x" in rec.text
