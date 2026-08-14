"""What every router leans on: lookups that 404, who the caller is, what their
role lets them do, and the SSE plumbing
"""

import json

from fastapi import HTTPException, Request

from backend.core import config
from backend.data import notes, projects, store, users


# Role sets come from permissions.grants in kndb.yaml, so a deployment can
# widen or narrow what each role may do without touching the routes.
WRITE = config.WRITE_ROLES
EDIT_OTHERS = config.EDIT_OTHERS_ROLES
MANAGE = config.MANAGE_ROLES
ADMIN = config.ADMIN_ROLES


async def get_source_or_404(sid: str) -> dict:
    row = await store.get_source(sid)
    if not row:
        raise HTTPException(404, f"source {sid} not found")
    return row

async def get_project_or_404(pid: str) -> dict:
    p = await projects.get_project(pid)
    if not p:
        raise HTTPException(404, f"project {pid} not found")
    return p

async def get_note_or_404(nid: str) -> dict:
    n = await notes.get(nid)
    if not n:
        raise HTTPException(404, f"note {nid} not found")
    return n

async def current_user(request: Request) -> str:
    """Caller's name, from the X-KNDB-User header or the kndb_user cookie.
    A name nobody has used yet is registered on the spot, and no name at all
    falls back to the default user: identity is claimed here, never proved
    """
    name = (request.headers.get("X-KNDB-User")
            or request.cookies.get("kndb_user") or "").strip()
    return (await users.ensure(name))["name"]

async def require_role(pid: str, user: str, *allowed: str) -> str:
    """Caller's role in the project, 403 when they are not a member at all.
    Naming roles narrows it to those, so calling it bare is the read check
    """
    role = await projects.member_role(pid, user)
    if role is None:
        raise HTTPException(403, "you are not a member of this project")
    if allowed and role not in allowed:
        raise HTTPException(
            403, f"this action needs {' or '.join(allowed)}; you are {role}")
    return role

async def writable_project(pid: str, user: str) -> dict:
    """The project, once the caller is known to be allowed to write in it
    """
    p = await get_project_or_404(pid)
    await require_role(pid, user, *WRITE)
    return p

def sse(events):
    """Wraps an async stream of JSON-able events as SSE data frames
    """
    async def gen():
        async for ev in events:
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return gen()

def sse_headers() -> dict:
    """Keeps a stream flowing token by token instead of arriving in one block.
    A cache or a buffering proxy in front of the app would otherwise hold it
    """
    return {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }

async def project_sources_rows(pid: str) -> list:
    """Public rows of the sources a project holds.
    A link whose source is gone is skipped rather than reported
    """
    srcs = []
    for sid in (await projects.get_project(pid))["source_ids"]:
        row = await store.get_source(sid)
        if row:
            srcs.append(store.to_public(row))
    return srcs
