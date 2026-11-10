"""轮次日志上下文：把 turn_id / session_id 通过 contextvars 带给日志行。

调用方只在轮次边界用 ``with turn_log_context(...)``，轮内所有日志调用保持
原样；``log_setup`` 的过滤器从 contextvars 取值写入 LogRecord，格式化时
统一加前缀。这样不需要给每个 logger 调用手工传字段，也不会漏传。
"""
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_turn_id: ContextVar[str] = ContextVar("zero_turn_id", default="")
_session_id: ContextVar[str] = ContextVar("zero_session_id", default="")


def current_turn_id() -> str:
    return _turn_id.get()


def current_session_id() -> str:
    return _session_id.get()


@contextmanager
def turn_log_context(turn_id: str, session_id: str) -> Iterator[None]:
    """在当前协程/线程上下文里设置轮次标识，退出时恢复原值。

    ``asyncio.to_thread`` 会复制当前 context，因此线程池里的写盘/工具日志
    同样能带上这两个字段；未进入轮次的日志保持空前缀。
    """
    turn_token = _turn_id.set(turn_id)
    session_token = _session_id.set(session_id)
    try:
        yield
    finally:
        _turn_id.reset(turn_token)
        _session_id.reset(session_token)
