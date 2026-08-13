"""
Build context for agent
"""
import platform

from typing import Any

from session.store import SessionStore
from tools.registry import ToolRegistry
from config import ZERO_ROOT


class Context:

    def __init__(
            self,
            session_id: str,
            channel: str,
            tool_registry: ToolRegistry | None = None,
    ):
        self.session_id = session_id
        self.channel = channel
        self.tool_registry = tool_registry

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
用户偏好/长期记忆/回话级记忆/按日期的记忆
    
{self._skills}    
"""

    @property
    def _runtime(self) -> str:
        return f"""## Runtime
{platform.system()}, {platform.machine()}, {platform.python_version()}
"""

    @property
    def _memory(self) -> str:
        return f"""## Memory Guidelines
"""

    @property
    def _skills(self) -> str:
        return f"""## Skill Guidelines
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
