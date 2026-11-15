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

    changes = tracker.finish()

    assert len(changes) == 1
    assert changes[0]["change"] == "group"
    assert changes[0]["blocked"] is True
    assert changes[0]["count"] == 1
    assert changes[0]["path"].endswith("node_modules")


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
    assert len(groups[0]["files"]) == 20
    assert groups[0]["hiddenCount"] == 280
    assert [item["path"] for item in singles] == [str(bulk / "uv.lock")]


def test_global_entry_cap_reports_hidden_count(tmp_path):
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
    hidden = [item for item in changes if item["change"] == "hidden"]

    assert len(stored) == 200
    assert hidden and hidden[0]["count"] == 100


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
