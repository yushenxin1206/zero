"""进程数据根目录的唯一来源：显式配置，惰性读取。

本模块只做路径算术：不读文件、不建目录、不实例化 Config。
进程启动时由 ``main()`` 调 ``configure()``；测试由 pytest fixture 调
``configure(tmp_path)``。组合根与进程边界（CLI / 工具 / 子命令）通过
``current()`` 取一次路径，再把 ``Paths`` 显式传给核心对象；核心域不在
调用点回读进程根。

``scope()`` 给同一进程内的多实例/集成测试一个轻量入口：``with
scope(root):`` 期间该协程上下文读到另一个根。没有 override 时回落到
``configure()`` 设的进程根，因此后台线程（如飞书 WS 线程）行为不变。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


def default_root() -> Path:
    """默认数据根：``~/.zero``。只做路径拼接，不触碰磁盘。"""
    return Path.home() / ".zero"


@dataclass(frozen=True)
class Paths:
    """一棵数据根下的全部路径视图（纯派生，无 IO）。"""
    root: Path
    # 项目级记忆的显式覆盖：默认 None 表示按运行目录推导。嵌入式宿主或
    # 测试需要把项目记忆移出仓库时使用，避免 monkeypatch 模块函数。
    project_memory: Path | None = None

    @property
    def config(self) -> Path:
        return self.root / "config.json"

    @property
    def memory(self) -> Path:
        return self.root / "memory.md"

    @property
    def sessions(self) -> Path:
        return self.root / "sessions"

    @property
    def scheduler(self) -> Path:
        return self.root / "scheduler"

    @property
    def tasks(self) -> Path:
        return self.scheduler / "tasks.jsonl"

    @property
    def profiles(self) -> Path:
        return self.root / "run"

    @property
    def profile_file(self) -> Path:
        return self.root / "profile.json"

    @property
    def maintenance(self) -> Path:
        return self.root / "maintenance"

    @property
    def maintenance_state(self) -> Path:
        return self.maintenance / "state.json"


_global_paths = Paths(default_root())
_override: ContextVar[Paths | None] = ContextVar("zero_paths", default=None)


def configure(root: Path | None = None, *,
              project_memory: Path | None = None) -> Paths:
    """设置进程级数据根。由 ``main()`` 或测试 fixture 显式调用。"""
    global _global_paths
    _global_paths = Paths(
        Path(root) if root is not None else default_root(),
        project_memory=project_memory,
    )
    return _global_paths


@contextmanager
def scope(root: Path, *, project_memory: Path | None = None) -> Iterator[Paths]:
    """在当前协程上下文内临时切换到另一个数据根。

    异步任务在创建时复制上下文，因此两个任务可以各持一个根并发运行；
    未进入 scope 的线程仍读进程根。
    """
    paths = Paths(Path(root), project_memory=project_memory)
    token = _override.set(paths)
    try:
        yield paths
    finally:
        _override.reset(token)


def current() -> Paths:
    """当前生效的数据根：协程级 override 优先，否则进程根。"""
    return _override.get() or _global_paths
