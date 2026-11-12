"""启动参数：默认 web，多通道开关可组合。"""
import sys

from zero.main import parse_args


def _parse(monkeypatch, *flags: str):
    monkeypatch.setattr(sys, "argv", ["zero", *flags])
    return parse_args()


def test_no_flags_defaults_to_web_only(monkeypatch):
    args = _parse(monkeypatch)
    assert args.web and not args.cli and not args.feishu


def test_flags_combine(monkeypatch):
    args = _parse(monkeypatch, "--web", "--cli", "--feishu")
    assert args.web and args.cli and args.feishu


def test_cli_only(monkeypatch):
    args = _parse(monkeypatch, "--cli")
    assert not args.web and args.cli and not args.feishu


def test_feishu_only(monkeypatch):
    args = _parse(monkeypatch, "--feishu")
    assert not args.web and not args.cli and args.feishu
