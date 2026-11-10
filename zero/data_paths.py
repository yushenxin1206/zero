"""数据根的路径视图：纯推导，无 IO、无全局状态。

``DataPaths`` 只做路径算术：不读文件、不建目录、不实例化 Config，也不持有
“当前根”这类可变状态。数据根由组合根或测试显式构造并向下注入；CLI 入口
用 ``DataPaths.default()`` 构造一份。同进程多实例直接构造各自的
``DataPaths``，互不影响。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DataPaths:
    """一棵数据根下的全部路径视图（纯派生，无 IO）。"""
    root: Path
    # 项目级记忆的显式覆盖：默认 None 表示按运行目录推导。嵌入式宿主或
    # 测试需要把项目记忆移出仓库时使用，避免 monkeypatch 模块函数。
    project_memory: Path | None = None

    @classmethod
    def default(cls) -> DataPaths:
        """默认数据根（``~/.zero``）的路径视图；不缓存，避免 import 期冻结。"""
        return cls(Path.home() / ".zero")

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
