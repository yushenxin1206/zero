"""技能发现：包内 + 用户技能根合并、同名覆盖、可写权限。"""
from pathlib import Path

from zero.data_paths import DataPaths
from zero.models import ToolContext
from zero.permission.envelope import PermissionState
from zero.session.store import SessionStore
from zero.skill_catalog import (
    SOURCE_BUILTIN, SOURCE_USER, list_skills)


def write_skill(root: Path, name: str, description: str) -> None:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "skill.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nbody\n",
        encoding="utf-8")


def test_user_skills_merge_and_override(tmp_path):
    user_root = tmp_path / "skills"
    write_skill(user_root, "new-skill", "user only")
    write_skill(user_root, "plan", "user plan")

    skills = {item.name: item for item in list_skills(user_root)}

    assert skills["new-skill"].source == SOURCE_USER
    assert skills["new-skill"].overrides is False
    assert skills["plan"].source == SOURCE_USER
    assert skills["plan"].overrides is True
    assert skills["plan"].description == "user plan"
    assert skills["session-history"].source == SOURCE_BUILTIN


def test_missing_user_root_falls_back_to_builtin(tmp_path):
    skills = list_skills(tmp_path / "missing")
    names = {item.name for item in skills}
    assert {"plan", "session-history", "web-search", "skill-authoring"} <= names


def test_user_skills_root_is_writable_without_approval(tmp_path):
    data_paths = DataPaths(tmp_path / "data")
    state = PermissionState(
        data_paths, SessionStore(data_paths.sessions))
    ctx = ToolContext(channel="web", session_id="s1")
    target = data_paths.skills / "new-skill" / "skill.md"

    verdict = state.check_writable(str(target), ctx)

    assert verdict.ok is True
    assert verdict.tier == "P"
    assert data_paths.skills == data_paths.root / "skills"


def test_packaged_bundle_stays_read_only(tmp_path, monkeypatch):
    bundle = tmp_path / "Zero.app"
    monkeypatch.setenv("ZERO_APP_BUNDLE", str(bundle))
    data_paths = DataPaths(tmp_path / "data")
    state = PermissionState(
        data_paths, SessionStore(data_paths.sessions))
    ctx = ToolContext(channel="web", session_id="s1")

    inside = (bundle / "Contents" / "Resources" / "app" / "zero"
              / "skills" / "plan" / "skill.md")
    verdict = state.check_writable(str(inside), ctx)

    assert verdict.ok is False
    assert verdict.tier == "D"
    assert state.check_writable(
        str(data_paths.skills / "plan" / "skill.md"), ctx).ok is True
    # 即使有人把应用包设为工作目录，它也不会进入沙箱写集合
    forced = ToolContext(
        channel="web", session_id="s1", working_dir=str(bundle))
    assert all(not root.is_relative_to(bundle)
               for root in state.envelope(forced).write_roots())
