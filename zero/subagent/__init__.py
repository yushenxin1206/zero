"""
Subagent package: background parallel execution of delegated tasks.

The main conversation delegates self-contained tasks via the `subagent` tool
(tools/subagent.py), which hands them to SubagentManager here. Each task runs
as an isolated single-shot agent (core.runner.run_subagent) and, when done,
its result is dispatched back into the message bus as a new turn.
"""
from zero.subagent.manager import SubagentManager

__all__ = ["SubagentManager"]