"""进程数据根目录的唯一来源：显式配置，惰性读取。

本模块只做路径算术：不读文件、不建目录、不实例化 Config。
进程启动时由 ``main()`` 调 ``configure()``；测试由 pytest fixture 调
``configure(tmp_path)``。组合根与进程边界（CLI / 工具 / 子命令）通过
``current()`` 取一次路径，再把 ``DataPaths`` 显式传给核心对象；核心域不在
调用点回读进程根。

同进程多实例不依赖隐式全局：直接构造 ``DataPaths`` 并注入即可。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


def default_root() -> Path:
    """默认数据根：``~/.zero``。只做路径拼接，不触碰磁盘。"""
    return Path.home() / ".zero"


@dataclass(frozen=True)
class DataPaths:
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


_global_paths = DataPaths(default_root())


def configure(root: Path | None = None, *,
              project_memory: Path | None = None) -> DataPaths:
    """设置进程级数据根。由 ``main()`` 或测试 fixture 显式调用。"""
    global _global_paths
    _global_paths = DataPaths(
        Path(root) if root is not None else default_root(),
        project_memory=project_memory,
    )
    return _global_paths


def current() -> DataPaths:
    """当前进程配置的数据根。只供 CLI 入口与测试边界使用。"""
    return _global_paths
