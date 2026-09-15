"""
Build context for agent
"""
import platform

from typing import Any
from pathlib import Path

import frontmatter

from datacls import ToolContext
from session.store import SessionStore
from tools.registry import ToolRegistry
from config import ZERO_ROOT


# 包安装目录（skills 等内置资源所在位置），与运行目录无关
PKG_ROOT = Path(__file__).parent.parent


def get_cwd() -> Path:
    """实时获取 zero 命令运行时所在的目录。"""
    return Path.cwd()


class Context:

    def __init__(
            self,
            session_id: str,
            channel: str,
            tool_registry: ToolRegistry | None = None,
            tool_context: ToolContext | None = None,
    ):
        self.session_id = session_id
        self.channel = channel
        self.tool_registry = tool_registry
        self.tool_context = tool_context or ToolContext(
            channel=channel,
            session_id=session_id,
        )

    @property
    def _prompt(self) -> str:
        return f"""You are Zero, a professional and efficient assistant. Your core principle is to help users accomplish their goals with minimal friction.
        
{self._runtime}

## Identity & Tone
- Respond concisely and directly. Avoid preamble, filler, or repetition.
- Use a professional but warm tone — no over-enthusiasm, no cold indifference.
- Match the user's language. If the user writes in Chinese, respond in Chinese; if in English, respond in English.

## How You Work
- Answer simple questions directly with clear, actionable information.
- For tasks requiring execution: use tools to get things done. Prefer doing over describing.
- When a task is ambiguous, ask one clarifying question rather than making assumptions.
- Break complex problems into logical steps, but don't over-explain — just execute.

## Tool Usage
- Prefer tools to text when they yield better results.
- Run independent tool calls in parallel.
- Verify results before presenting them.
- Tool results exceeding 10000 characters are truncated.

## Boundaries
- Never fabricate information. If you don't know, say so and offer alternatives.
- Don't take harmful actions (delete files, modify critical configs) without explicit confirmation.
- Respect privacy and security — never expose sensitive data.

{self._memory}
    
{self._skills}    
"""

    @property
    def _runtime(self) -> str:
        return f"""## Runtime
{platform.system()}, {platform.machine()}, {platform.python_version()}
"""

    @property
    def _memory(self) -> str:
        # 用户偏好/长期记忆/回话级记忆/按日期的记忆
        # 用户偏好 user.md
        # 长期记忆 agent.md
        # 会话级记忆
        memory_path = ZERO_ROOT / 'memory.md'

        if not memory_path.exists():
            Path.mkdir(memory_path.parent, parents=True, exist_ok=True)
            with open(memory_path, 'w+') as f:
                f.write("I am Zero, a professional and efficient assistant.")

        with open(memory_path, encoding="utf-8") as f:
            memory = f.read()
        return f"""## Memory Guidelines
Your long-term memory is shown below. It is stored at {memory_path}, and you can update it when necessary.

<memory>
{memory}
</memory>

Use the above memory to personalize your response. If the memory contradicts the user's current request, prioritize the current request but note the discrepancy.
"""

    @staticmethod
    def _parse_skill_meta(skill_file: Path) -> dict[str, str]:
        """Parse YAML frontmatter of a skill.md file for name and description."""
        try:
            post = frontmatter.load(skill_file)
        except (OSError, ValueError):
            return {}
        meta = post.metadata or {}
        return {k: str(v) for k, v in meta.items()}

    @property
    def _skills(self) -> str:
        skills_path = PKG_ROOT / "skills"
        entries: list[str] = []

        if skills_path.is_dir():
            for skill_dir in sorted(skills_path.iterdir()):
                if not skill_dir.is_dir():
                    continue
                skill_file = skill_dir / "skill.md"
                if not skill_file.exists():
                    continue
                meta = self._parse_skill_meta(skill_file)
                entries.append(
                    f"  <skill>\n"
                    f"    <name>{meta.get('name', '')}</name>\n"
                    f"    <description>{meta.get('description', '')}</description>\n"
                    f"    <location>{skill_file}</location>\n"
                    f"  </skill>"
                )

        available_skills = "\n".join(entries)

        return f"""## Skill Guidelines
The following skills provide specialized instructions for specific tasks.
Use the read tool to load a skill's file when the task matches its description.
When a skill file references a relative path, resolve it against the skill directory (parent of SKILL.md / dirname of the path) and use that absolute path in tool commands.

<available_skills>
{available_skills}
</available_skills>
"""

    @property
    def messages(self) -> list[dict[str, Any]]:
        history = SessionStore.load_history(self.session_id, self.channel)
        msgs: list[dict[str, Any]] = [
            {
                "content": self._prompt,
                "role": "system"
            }
        ]
        for m in history:
            if m.sender == "user":
                msgs.append({"role": "user", "content": m.body.content})
            elif m.sender == "assistant":
                msg: dict[str, Any] = {"role": "assistant", "content": m.body.content}
                if m.body.tool_calls:
                    msg["tool_calls"] = [tc.model_dump() for tc in m.body.tool_calls]
                msgs.append(msg)
            elif m.sender == "tool":
                msgs.append({
                    "role": "tool",
                    "tool_call_id": m.body.tool_call_id,
                    "content": m.body.content,
                })
        return msgs

    @property
    def tools(self) -> list[dict[str, Any]] | None:
        if self.tool_registry is None:
            return None
        schemas = self.tool_registry.schemas()
        return schemas if schemas else None


if __name__ == "__main__":
    _skill_file = PKG_ROOT / "skills" / "web-search" / "skill.md"
    print(Context._parse_skill_meta(_skill_file))

