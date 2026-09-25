"""The agent's tools for moving work between the caller's workspaces.

Reads only for now: the targets the caller could transfer to. The transfer
tools themselves (note, snippet, and the heavy import) come later — they
will join this module.
"""

import json

from ...data import projects


async def _list_transfer_targets(pid: str, user: str):
    """The caller's personal workspace and every team they belong to — the
    same shape the transfer UI offers. The project the conversation runs in
    is marked, so the agent can talk about "this one" and "the others"
    """
    personal = await projects.personal_project(user)
    teams = await projects.list_projects(user=user, kind="team")
    out = {"personal": {"id": personal["id"], "name": personal["name"]},
           "projects": [{"id": p["id"], "name": p["name"],
                         "is_current": p["id"] == pid} for p in teams]}
    return json.dumps(out)


_list_transfer_targets_tool = {
    "security": "read",
    "wants_user": True,
    "function": _list_transfer_targets,
    "tool_dict": {
        "name": "list_transfer_targets",
        "description": "List the workspaces the calling user could transfer work to: their personal workspace and every team project they belong to. The current project is marked.",
        "parameters": {
            "type": "object",
            "properties": {

            }
        }
    }
}


TOOLS = (
    _list_transfer_targets_tool,
)