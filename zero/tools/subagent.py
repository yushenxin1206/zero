"""
Subagent delegation tool.

Lets the main conversation hand off one or more self-contained tasks to
background subagents that run in parallel and report their results back as
new turns. The tool returns immediately (it does not block on the work); the
SubagentManager owns execution and result dispatch.
"""
from zero.models import ToolContext
from zero.subagent.manager import SubagentManager
from zero.tools.base import Tool


class SubagentTool(Tool):

    def __init__(self, manager: SubagentManager):
        self._manager = manager

    @property
    def name(self):
        return "subagent"

    @property
    def description(self):
        return (
            "Delegate one or more independent tasks to background subagents "
            "that run in PARALLEL and report their results back to this "
            "conversation when finished. Use this for self-contained work that "
            "can proceed while you keep helping the user (e.g. researching "
            "several topics, summarizing multiple files). Each task runs in an "
            "isolated agent with the same tool set as this conversation, "
            "except `subagent` itself (no recursive delegation). Background "
            "execution is unattended: actions that would need approval fail "
            "instead of prompting, and shell commands run without network. "
            "This call returns immediately with the launched task ids — the "
            "results arrive later as new messages, so do NOT wait or repeat "
            "the work. Split only work that is genuinely independent; keep "
            "dependent steps in the main conversation."
        )

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "description": "Independent tasks to run in parallel, one subagent each.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "prompt": {
                                "type": "string",
                                "description": "Complete, self-contained instruction for the subagent. It has no access to this conversation, so include all needed context and state the expected output.",
                            },
                            "description": {
                                "type": "string",
                                "description": "Short human-readable label for this task (shown when the result comes back). Optional.",
                            },
                        },
                        "required": ["prompt"],
                    },
                },
            },
            "required": ["tasks"],
        }

    async def run(self, context: ToolContext | None = None, **kwargs):
        if context is None or not context.session_id:
            return {"success": False,
                    "error": "No session context available for subagent."}

        raw_tasks = kwargs.get("tasks") or []
        if not isinstance(raw_tasks, list) or not raw_tasks:
            return {"success": False,
                    "error": "Missing required argument: tasks (non-empty list)."}

        tasks: list[dict] = []
        for i, item in enumerate(raw_tasks):
            if not isinstance(item, dict):
                return {"success": False,
                        "error": f"tasks[{i}] must be an object."}
            prompt = (item.get("prompt") or "").strip()
            if not prompt:
                return {"success": False,
                        "error": f"tasks[{i}].prompt is required and must be non-empty."}
            tasks.append({
                "prompt": prompt,
                "description": (item.get("description") or "").strip(),
            })

        launched = self._manager.launch(tasks, tool_context=context)
        return {
            "success": True,
            "launched": launched,
            "message": (
                f"Launched {len(launched)} background subagent(s). They run in "
                f"parallel; each will report back as a separate message when done. "
                f"Do not wait for or duplicate this work."
            ),
        }