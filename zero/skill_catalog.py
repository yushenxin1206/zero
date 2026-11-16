"""技能发现与元数据解析。

两个来源：

- 包内技能：``zero/skills/<name>/skill.md``，随包分发、只读；
- 用户技能：``<数据根>/skills/<name>/skill.md``（默认 ``~/.zero/skills``），
  运行期可写，供用户或 agent 新增技能。

同名技能由用户目录覆盖包内版本，便于用户在不改应用包的前提下调整行为；
覆盖关系会记录在 ``SkillInfo.overrides``，供界面标明。技能正文是给模型读的
操作说明，文件头 YAML frontmatter 提供 ``name`` 与 ``description``；这里只做
只读发现，不执行任何技能脚本。
"""
from dataclasses import dataclass
from pathlib import Path

import frontmatter

BUILTIN_SKILLS_ROOT = Path(__file__).parent / "skills"
SOURCE_BUILTIN = "builtin"
SOURCE_USER = "user"


@dataclass(frozen=True)
class SkillInfo:
    """A discovered skill's metadata and its skill.md location."""
    name: str
    description: str
    location: Path
    source: str = SOURCE_BUILTIN
    # 用户技能覆盖了同名包内技能；界面据此标明覆盖来源
    overrides: bool = False


def skill_dirs(user_root: Path | None = None) -> list[tuple[str, Path]]:
    """按优先级返回技能目录：包内在前，用户目录在后（后者覆盖同名技能）。"""
    dirs = [(SOURCE_BUILTIN, BUILTIN_SKILLS_ROOT)]
    if user_root is not None:
        dirs.append((SOURCE_USER, Path(user_root)))
    return dirs


def parse_skill_meta(skill_file: Path) -> dict[str, str]:
    """Parse YAML frontmatter of a skill.md for name and description."""
    try:
        post = frontmatter.load(skill_file)
    except (OSError, ValueError):
        return {}
    # 走 frontmatter.Post 的映射接口，不直接碰 .metadata 属性——
    # 全仓库禁止核心层访问 Message/StreamEvent 的 metadata 口袋
    return {k: str(post[k]).strip() for k in post.keys()}


def read_skill_body(skill_file: Path) -> str:
    """skill.md 正文，已去掉 YAML frontmatter（元数据单独展示）。"""
    try:
        return frontmatter.load(skill_file).content
    except (OSError, ValueError):
        return ""


def list_skills(user_root: Path | None = None) -> list[SkillInfo]:
    """合并包内与用户技能；同名时用户目录胜出。

    覆盖规则按技能名（frontmatter ``name``，缺失时退回目录名）判定，
    与提示词里展示的名字一致，避免「目录覆盖了但名字还是旧的」。
    """
    found: dict[str, SkillInfo] = {}
    for source, root in skill_dirs(user_root):
        if not root.is_dir():
            continue
        for skill_file in sorted(root.glob("*/skill.md")):
            meta = parse_skill_meta(skill_file)
            name = meta.get("name") or skill_file.parent.name
            previous = found.get(name)
            found[name] = SkillInfo(
                name=name,
                description=meta.get("description", ""),
                location=skill_file,
                source=source,
                overrides=(
                    source == SOURCE_USER
                    and previous is not None
                    and previous.source == SOURCE_BUILTIN
                ),
            )
    return sorted(found.values(), key=lambda item: item.name)
