"""内置技能的发现与元数据解析。

技能以 ``zero/skills/<name>/skill.md`` 的形式随包分发：正文是给模型读的
操作说明，文件头的 YAML frontmatter 提供 ``name`` / ``description``。
这里只做只读发现，不执行任何技能脚本。
"""
from dataclasses import dataclass
from pathlib import Path

import frontmatter

SKILLS_ROOT = Path(__file__).parent / "skills"


@dataclass(frozen=True)
class SkillInfo:
    """A discovered skill's metadata and its skill.md location."""

    name: str
    description: str
    location: Path


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


def list_skills() -> list[SkillInfo]:
    """List package skills in stable name order (empty when none exist)."""
    if not SKILLS_ROOT.is_dir():
        return []
    skills: list[SkillInfo] = []
    for skill_file in sorted(SKILLS_ROOT.glob("*/skill.md")):
        meta = parse_skill_meta(skill_file)
        skills.append(SkillInfo(
            name=meta.get("name") or skill_file.parent.name,
            description=meta.get("description", ""),
            location=skill_file,
        ))
    return skills
