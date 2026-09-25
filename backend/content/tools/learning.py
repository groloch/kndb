"""The agent's tools over quizzes and play stats.

Gated per project: the ``available`` hook keeps these tools out of the
agent's schema unless the project's ``allow_quiz`` capability is on — the
first cut of the per-project modularity (a project without the learning tab
never offers them, and a call to them is refused as unknown).
"""

import json

from ...data import projects
from ...data import quiz as quiz_store


async def _available(pid: str) -> bool:
    """Learning tools exist only where the learning module is on
    """
    caps = await projects.get_capabilities(pid)
    return bool(caps.get("allow_quiz"))


async def _get_quiz(pid: str, source_id: str):
    if not await projects.has_source(pid, source_id):
        return f"Source {source_id} is not in this project."
    q = await quiz_store.read_quiz(pid, source_id)
    out = json.dumps(q)
    if len(out) > 15000:
        return f"The quiz holds {len(q.get('questions', []))} questions; too much to dump at once. Ask about single questions instead."
    return out


async def _get_quiz_stats(pid: str, source_id: str):
    if not await projects.has_source(pid, source_id):
        return f"Source {source_id} is not in this project."
    st = await quiz_store.read_stats(pid, source_id)
    if not st:
        return "No quiz stats for this source — it was never played here."
    out = json.dumps(st)
    if len(out) > 15000:
        out = out[:15000] + " …(truncated)"
    return out


_get_quiz_tool = {
    "security": "read",
    "available": _available,
    "function": _get_quiz,
    "tool_dict": {
        "name": "get_quiz",
        "description": "Read the quiz saved for a source in this project: its questions with their choices and correct answers. Lets you quiz the user or explain the questions.",
        "parameters": {
            "type": "object",
            "properties": {
                "source_id": {"type": "string"}
            },
            "required": ["source_id"]
        }
    }
}
_get_quiz_stats_tool = {
    "security": "read",
    "available": _available,
    "function": _get_quiz_stats,
    "tool_dict": {
        "name": "get_quiz_stats",
        "description": "Read the spaced-repetition play statistics of a source's quiz in this project: how many questions, what was answered when and how well.",
        "parameters": {
            "type": "object",
            "properties": {
                "source_id": {"type": "string"}
            },
            "required": ["source_id"]
        }
    }
}


TOOLS = (
    _get_quiz_tool,
    _get_quiz_stats_tool,
)