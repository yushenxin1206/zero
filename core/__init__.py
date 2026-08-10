"""
Core package for agent runner, loop, and context.
"""

from dataclasses import dataclass
from pathlib import Path


@dataclass
class AgentConfig:
    """Runtime configuration shared by Runner and Loop."""

    workspace: Path
    provider: str
    model: str
    max_tokens: int = 8192
    context_window_tokens: int = 65536
    temperature: float | int = 0.1
    max_loops: int = 20
