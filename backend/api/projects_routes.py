from typing import Optional

from fastapi import APIRouter, HTTPException

from backend.api.deps import get_project_or_404, project_sources_rows
from backend.data import projects, store


router = APIRouter()


@router.get("/api/projects")
async def list_projects(q: str = ""):
    return {"ok": True, "projects": await projects.list_projects(q)}

@router.post("/api/projects")
async def create_project(body: Optional[dict] = None):
    body = body or {}
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "project name required")
    p = await projects.create_project(name, (body.get("description") or "").strip())
    return {"ok": True, "project": p}

@router.get("/api/projects/{pid}")
async def project_detail(pid: str):
    return {"ok": True, "project": await get_project_or_404(pid)}

@router.post("/api/projects/{pid}")
async def update_project(pid: str, body: Optional[dict] = None):
    await get_project_or_404(pid)
    body = body or {}
    p = await projects.update_project(
        pid,
        name=(body.get("name") or "").strip() or None,
        description=(body.get("description") or "").strip() or None,
        completed=body.get("completed"),
    )
    return {"ok": True, "project": p}

@router.delete("/api/projects/{pid}")
async def delete_project(pid: str):
    await get_project_or_404(pid)
    await projects.delete_project(pid)
    return {"ok": True}

@router.get("/api/projects/{pid}/members")
async def project_members(pid: str):
    await get_project_or_404(pid)
    return {"ok": True, "members": await projects.list_members(pid)}

@router.post("/api/projects/{pid}/members")
async def add_member(pid: str, body: Optional[dict] = None):
    await get_project_or_404(pid)
    body = body or {}
    name = (body.get("name") or "").strip()
    role = (body.get("role") or "contributor").strip()
    if not name:
        raise HTTPException(400, "member name required")
    if role not in projects.ROLES:
        raise HTTPException(400, f"invalid role; choose from {sorted(projects.ROLES)}")
    try:
        m = await projects.add_member(pid, name, role)
    except ValueError as e:
        raise HTTPException(409, str(e))
    if m is None:
        raise HTTPException(409, f"member {name!r} already in the project")
    return {"ok": True, "member": m, "members": await projects.list_members(pid)}

@router.post("/api/projects/{pid}/members/{name}/role")
async def set_member_role(pid: str, name: str, body: Optional[dict] = None):
    await get_project_or_404(pid)
    body = body or {}
    role = (body.get("role") or "contributor").strip()
    if role not in projects.ROLES:
        raise HTTPException(400, f"invalid role; choose from {sorted(projects.ROLES)}")
    try:
        ok = await projects.set_member_role(pid, name, role)
    except ValueError as e:
        raise HTTPException(409, str(e))
    if not ok:
        raise HTTPException(404, f"member {name!r} not found")
    return {"ok": True, "members": await projects.list_members(pid)}

@router.delete("/api/projects/{pid}/members/{name}")
async def remove_member(pid: str, name: str):
    await get_project_or_404(pid)
    try:
        ok = await projects.remove_member(pid, name)
    except ValueError as e:
        raise HTTPException(409, str(e))
    if not ok:
        raise HTTPException(404, f"member {name!r} not found")
    return {"ok": True, "members": await projects.list_members(pid)}

@router.get("/api/projects/{pid}/sources")
async def project_sources(pid: str):
    await get_project_or_404(pid)
    return {"ok": True, "sources": await project_sources_rows(pid)}

@router.post("/api/projects/{pid}/sources")
async def add_project_source(pid: str, body: Optional[dict] = None):
    await get_project_or_404(pid)
    body = body or {}
    sid = body.get("source_id")
    if not sid or not await store.get_source(sid):
        raise HTTPException(404, "source not found")
    await projects.add_source(pid, sid)
    return {"ok": True, "sources": await project_sources_rows(pid)}

@router.delete("/api/projects/{pid}/sources/{sid}")
async def remove_project_source(pid: str, sid: str):
    await get_project_or_404(pid)
    if not await projects.remove_source(pid, sid):
        raise HTTPException(404, "source not in project")
    return {"ok": True, "sources": await project_sources_rows(pid)}
