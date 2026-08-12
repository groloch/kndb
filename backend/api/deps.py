import json

from fastapi import HTTPException, Request

from backend.data import notes, projects, store, users


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
    name = (request.headers.get("X-KNDB-User")
            or request.cookies.get("kndb_user") or "").strip()
    return (await users.ensure(name))["name"]

async def require_role(pid: str, user: str, *allowed: str) -> str:
    role = await projects.member_role(pid, user)
    if role is None:
        raise HTTPException(403, "you are not a member of this project")
    if allowed and role not in allowed:
        raise HTTPException(
            403, f"this action needs {' or '.join(allowed)}; you are {role}")
    return role

async def writable_project(pid: str, user: str) -> dict:
    p = await get_project_or_404(pid)
    await require_role(pid, user, "owner", "maintainer", "contributor")
    return p

def sse(events):
    async def gen():
        async for ev in events:
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return gen()

def sse_headers() -> dict:
    return {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }

async def project_sources_rows(pid: str) -> list:
    srcs = []
    for sid in (await projects.get_project(pid))["source_ids"]:
        row = await store.get_source(sid)
        if row:
            srcs.append(store.to_public(row))
    return srcs
