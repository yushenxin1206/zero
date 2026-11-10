"""Shared pytest setup.

Per-test filesystem isolation goes through an explicit ``DataPaths``: the
autouse fixture registers a throwaway root before the test body runs, and the
helpers in ``tests/support.py`` read that registration. No storage module
constants are monkeypatched, and the developer's real ``~/.zero`` is never
read or written.

The process HOME is deliberately left untouched: the security tests classify
paths relative to the user's home (e.g. "outside the envelope but in HOME"),
and pointing HOME into TMPDIR would make those paths fall into the writable
TMPDIR root and invalidate the assertions.
"""
import pytest

from tests.support import current, set_data_paths

from zero.data_paths import DataPaths
from zero.permission.envelope import PermissionState
from zero.scheduler.store import SchedulerStore
from zero.session.store import SessionStore


@pytest.fixture(autouse=True)
def isolated_state(tmp_path):
    """每例测试独立数据根；登记发生在测试体（及其线程）启动之前。"""
    set_data_paths(DataPaths(
        tmp_path / ".zero",
        project_memory=tmp_path / "project_memory.md",
    ))
    return tmp_path


@pytest.fixture
def session_store(isolated_state):
    """测试边界：按当前隔离根构造会话存储，和组合根的构造方式一致。"""
    return SessionStore(current().sessions)


@pytest.fixture
def scheduler_store(isolated_state):
    """测试边界：按当前隔离根构造调度任务存储。"""
    return SchedulerStore(current().tasks)


@pytest.fixture
def permission(isolated_state):
    """测试边界：按当前隔离根构造权限状态。"""
    return PermissionState(current(), SessionStore(current().sessions))
