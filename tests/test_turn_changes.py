"""轮次改动清单：快照对比、工具记账与根目录约束。"""

import shutil
import subprocess
from pathlib import Path

from zero.core.turn_changes import (
    CHANGE_ADDED,
    CHANGE_DELETED,
    CHANGE_MODIFIED,
    TurnChanges,
    build_turn_changes,
)


def _tracker(root: Path) -> TurnChanges:
    return TurnChanges(roots=[root.resolve()])


def test_snapshot_detects_add_modify_delete(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    keep = workspace / "keep.txt"
    keep.write_text("before", encoding="utf-8")
    gone = workspace / "gone.txt"
    gone.write_text("bye", encoding="utf-8")

    tracker = _tracker(workspace)
    tracker.start()

    keep.write_text("after, longer", encoding="utf-8")
    gone.unlink()
    (workspace / "new.txt").write_text("new", encoding="utf-8")

    changes = {item["path"]: item["change"] for item in tracker.finish()}

    assert changes[str(keep)] == CHANGE_MODIFIED
    assert changes[str(gone)] == CHANGE_DELETED
    assert changes[str(workspace / "new.txt")] == CHANGE_ADDED


def test_untouched_files_are_absent(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "idle.txt").write_text("idle", encoding="utf-8")

    tracker = _tracker(workspace)
    tracker.start()

    assert tracker.finish() == []


def test_record_write_counts_tool_touched_file(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "tool.txt"
    target.write_text("same", encoding="utf-8")

    tracker = _tracker(workspace)
    tracker.start()
    tracker.record(str(target))

    changes = {item["path"]: item["change"] for item in tracker.finish()}
    assert changes[str(target)] == CHANGE_MODIFIED


def test_record_ignores_paths_outside_roots(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")

    tracker = _tracker(workspace)
    tracker.start()
    tracker.record(str(outside))

    assert tracker.finish() == []


def test_dependency_dirs_only_report_group_summary(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tracker = _tracker(workspace)
    tracker.start()
    deps = workspace / "node_modules"
    deps.mkdir()
    (deps / "dep.js").write_text("x", encoding="utf-8")
    (deps / "dep2.js").write_text("x", encoding="utf-8")

    changes = tracker.finish()

    assert len(changes) == 1
    assert changes[0]["change"] == "group"
    assert changes[0]["blocked"] is True
    assert changes[0]["count"] == 2
    assert changes[0]["path"].endswith("node_modules")


def test_single_file_in_dependency_dir_stays_explicit(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tracker = _tracker(workspace)
    tracker.start()
    deps = workspace / "node_modules"
    deps.mkdir()
    (deps / "dep.js").write_text("x", encoding="utf-8")

    changes = tracker.finish()

    # 单文件目录不成组，直接给出文件自己的绝对路径
    assert len(changes) == 1
    assert changes[0]["change"] == CHANGE_MODIFIED
    assert changes[0]["path"] == str(deps / "dep.js")


def test_git_repo_uses_status_for_candidates(tmp_path):
    if shutil.which("git") is None:
        return
    workspace = tmp_path / "repo"
    workspace.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(workspace), *args], check=True,
                       capture_output=True, text=True)

    git("init")
    git("config", "user.email", "zero@example.com")
    git("config", "user.name", "zero")
    tracked = workspace / "tracked.txt"
    tracked.write_text("one", encoding="utf-8")
    doomed = workspace / "doomed.txt"
    doomed.write_text("two", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "init")

    tracker = _tracker(workspace)
    assert tracker.start() is None
    assert tracker.git is True

    tracked.write_text("one changed", encoding="utf-8")
    doomed.unlink()
    (workspace / "fresh.txt").write_text("new", encoding="utf-8")

    changes = {item["path"]: item["change"] for item in tracker.finish()}

    assert changes[str(tracked)] == CHANGE_MODIFIED
    assert changes[str(doomed)] == CHANGE_DELETED
    assert changes[str(workspace / "fresh.txt")] == CHANGE_ADDED


def test_compacts_large_directory_and_keeps_lockfile(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    bulk = workspace / "bulk"
    bulk.mkdir()
    tracker = _tracker(workspace)
    tracker.start()
    for index in range(300):
        (bulk / f"file-{index:03d}.txt").write_text("x", encoding="utf-8")
    (bulk / "uv.lock").write_text("lock", encoding="utf-8")

    changes = tracker.finish()
    groups = [item for item in changes if item["change"] == "group"]
    singles = [item for item in changes if item["change"] != "group"]

    assert len(groups) == 1
    assert groups[0]["count"] == 300
    # 超阈值只留“目录 + 数量”，不再展开明细
    assert groups[0]["files"] == []
    assert groups[0]["hiddenCount"] == 300
    assert [item["path"] for item in singles] == [str(bulk / "uv.lock")]


def test_global_entry_cap_groups_overflow_by_parent(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tracker = _tracker(workspace)
    tracker.start()
    for bucket in range(30):
        directory = workspace / f"d{bucket:02d}"
        directory.mkdir()
        for index in range(10):
            (directory / f"f{index}.txt").write_text("x", encoding="utf-8")

    changes = tracker.finish()
    stored = [
        item for item in changes
        if item["change"] != "group" and item["change"] != "hidden"]
    groups = [item for item in changes if item["change"] == "group"]
    hidden = [item for item in changes if item["change"] == "hidden"]

    assert len(stored) == 20
    # 超预算的 280 个文件按父目录上提成一行，且不再重复汇总
    assert len(groups) == 1
    assert groups[0]["count"] == 280
    assert hidden == []


def test_single_file_directory_is_not_grouped(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tracker = _tracker(workspace)
    tracker.start()
    # 两个目录各 10 条刚好填满 20 条预算，让后面的文件进入超预算聚合
    for bucket in range(2):
        directory = workspace / f"d{bucket}"
        directory.mkdir()
        for index in range(10):
            (directory / f"f{index:02d}.py").write_text("x", encoding="utf-8")
    lonely = workspace / "d2"
    lonely.mkdir()
    alone = lonely / "alone.py"
    alone.write_text("x", encoding="utf-8")
    crowded = workspace / "d3"
    crowded.mkdir()
    for index in range(9):
        (crowded / f"g{index:02d}.py").write_text("x", encoding="utf-8")

    changes = tracker.finish()
    groups = [item for item in changes if item["change"] == "group"]

    assert changes[0]["change"] == CHANGE_ADDED
    assert all(item["count"] != 1 for item in groups)
    assert str(alone) in [item["path"] for item in changes]
    assert any(item["count"] == 9 for item in groups)


def test_venv_prefixed_dir_reports_single_summary(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tracker = _tracker(workspace)
    tracker.start()
    packages = workspace / "venv-pptx" / "lib" / "python3.14" / "site-packages"
    packages.mkdir(parents=True)
    for name in ("rich", "pygments", "markdown_it"):
        target = packages / name
        target.mkdir()
        (target / "__init__.py").write_text("x", encoding="utf-8")

    changes = tracker.finish()

    groups = [item for item in changes if item["change"] == "group"]
    assert len(groups) == 1
    assert groups[0]["blocked"] is True
    assert groups[0]["path"].endswith("venv-pptx")
    assert groups[0]["count"] == 3
    assert all(item["change"] == "group" for item in changes)


def test_site_packages_outside_venv_reports_summary(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tracker = _tracker(workspace)
    tracker.start()
    packages = workspace / "env" / "lib" / "python3.12" / "site-packages"
    packages.mkdir(parents=True)
    (packages / "dep.py").write_text("x", encoding="utf-8")
    (packages / "dep2.py").write_text("x", encoding="utf-8")

    changes = tracker.finish()

    assert len(changes) == 1
    assert changes[0]["change"] == "group"
    assert changes[0]["blocked"] is True
    assert changes[0]["path"].endswith("site-packages")


def test_git_repo_filters_dependency_dirs(tmp_path):
    if shutil.which("git") is None:
        return
    workspace = tmp_path / "repo"
    workspace.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(workspace), *args], check=True,
                       capture_output=True, text=True)

    git("init")
    git("config", "user.email", "zero@example.com")
    git("config", "user.name", "zero")
    (workspace / "main.py").write_text("print(1)\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "init")

    tracker = _tracker(workspace)
    tracker.start()
    packages = workspace / "venv-pptx" / "lib" / "site-packages"
    packages.mkdir(parents=True)
    (packages / "a.py").write_text("x", encoding="utf-8")
    (packages / "b.py").write_text("x", encoding="utf-8")

    changes = tracker.finish()

    assert len(changes) == 1
    assert changes[0]["change"] == "group"
    assert changes[0]["blocked"] is True
    assert changes[0]["path"].endswith("venv-pptx")
    assert changes[0]["count"] == 2


def test_human_source_files_sort_before_artifacts(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tracker = _tracker(workspace)
    tracker.start()
    source = workspace / "main.py"
    source.write_text("x", encoding="utf-8")
    artifact = workspace / "assets" / "deep" / "nested" / "blob.bin"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"\x00")

    changes = tracker.finish()

    paths = [item["path"] for item in changes]
    assert paths.index(str(source)) < paths.index(str(artifact))


def test_file_changes_never_enter_model_context():
    from zero.models import MessageBody, MessageOut

    message = MessageOut(
        session_id="s", turn_id="t",
        body=MessageBody(
            content="改好了",
            file_changes=[{"path": "/tmp/a.txt", "change": "added"}]))
    payload = message.to_openai()

    assert payload["content"] == "改好了"
    assert "file_changes" not in payload
    assert "fileChanges" not in payload


def test_build_turn_changes_uses_only_workspace_and_session_roots():
    class Envelope:
        workspace = "/tmp/ws"
        session_roots = ["/tmp/granted"]
        extra_roots = ["/tmp/always"]
        session_dir = "/tmp/session"

    class Permission:
        def envelope(self, context):
            return Envelope()

    tracker = build_turn_changes(Permission(), object())

    expected = [str(Path("/tmp/ws").resolve()),
                str(Path("/tmp/granted").resolve())]
    assert [str(root) for root in tracker.roots] == expected
