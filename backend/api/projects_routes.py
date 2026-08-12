from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from backend.api.deps import (current_user, get_project_or_404,
                              get_source_or_404, require_role,
                              writable_project)
from backend.data import notes, projects
from backend.data import quiz as quiz_store
from backend.data import store


router = APIRouter()


@router.get("/api/me")
async def whoami(user: str = Depends(current_user)):
    from backend.data import users

    personal = await projects.personal_project(user)
    return {"ok": True, "user": await users.get(user),
            "users": await users.list_users(),
            "personal_project": personal}

@router.post("/api/users")
async def create_user(body: Optional[dict] = None):
    body = body or {}
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "user name required")
    from backend.data import users

    return {"ok": True, "user": await users.create(name, body.get("display_name") or "")}

@router.get("/api/projects")
async def list_projects(q: str = "", mine: bool = True,
                        user: str = Depends(current_user)):
    return {"ok": True,
            "projects": await projects.list_projects(q, user=user if mine else "")}

@router.post("/api/projects")
async def create_project(body: Optional[dict] = None,
                         user: str = Depends(current_user)):
    body = body or {}
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "project name required")
    p = await projects.create_project(name, (body.get("description") or "").strip(),
                                      owner=user)
    return {"ok": True, "project": p}

@router.get("/api/projects/{pid}")
async def project_detail(pid: str, user: str = Depends(current_user)):
    p = await get_project_or_404(pid)
    await require_role(pid, user)
    return {"ok": True, "project": p}

@router.post("/api/projects/{pid}")
async def update_project(pid: str, body: Optional[dict] = None,
                         user: str = Depends(current_user)):
    await get_project_or_404(pid)
    await require_role(pid, user, "owner", "maintainer")
    body = body or {}
    caps = (body.get("capabilities") or {}) if isinstance(body.get("capabilities"), dict) else {}
    p = await projects.update_project(
        pid,
        name=(body.get("name") or "").strip() or None,
        description=(body.get("description") or "").strip() or None,
        completed=body.get("completed"),
        **{k: caps.get(k) for k in ("allow_quiz", "multi_notes",
                                    "show_blame", "auto_import")},
    )
    return {"ok": True, "project": p}

@router.delete("/api/projects/{pid}")
async def delete_project(pid: str, user: str = Depends(current_user)):
    await get_project_or_404(pid)
    await require_role(pid, user, "owner")
    try:
        await projects.delete_project(pid)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}

@router.get("/api/projects/{pid}/members")
async def project_members(pid: str, user: str = Depends(current_user)):
    await get_project_or_404(pid)
    await require_role(pid, user)
    return {"ok": True, "members": await projects.list_members(pid)}

@router.post("/api/projects/{pid}/members")
async def add_member(pid: str, body: Optional[dict] = None,
                     user: str = Depends(current_user)):
    await get_project_or_404(pid)
    await require_role(pid, user, "owner", "maintainer")
    body = body or {}
    name = (body.get("name") or "").strip()
    role = (body.get("role") or "contributor").strip()
    if not name:
        raise HTTPException(400, "member name required")
    if role not in projects.ROLES:
        raise HTTPException(400, f"invalid role; choose from {sorted(projects.ROLES)}")
    from backend.data import users

    await users.ensure(name)  # a member must exist as a user to own text
    try:
        m = await projects.add_member(pid, name, role)
    except ValueError as e:
        raise HTTPException(409, str(e))
    if m is None:
        raise HTTPException(409, f"member {name!r} already in the project")
    return {"ok": True, "member": m, "members": await projects.list_members(pid)}

@router.post("/api/projects/{pid}/members/{name}/role")
async def set_member_role(pid: str, name: str, body: Optional[dict] = None,
                          user: str = Depends(current_user)):
    await get_project_or_404(pid)
    await require_role(pid, user, "owner", "maintainer")
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
async def remove_member(pid: str, name: str, user: str = Depends(current_user)):
    await get_project_or_404(pid)
    await require_role(pid, user, "owner", "maintainer")
    try:
        ok = await projects.remove_member(pid, name)
    except ValueError as e:
        raise HTTPException(409, str(e))
    if not ok:
        raise HTTPException(404, f"member {name!r} not found")
    return {"ok": True, "members": await projects.list_members(pid)}

@router.get("/api/projects/{pid}/sources")
async def project_sources(pid: str, q: str = "", user: str = Depends(current_user)):
    await get_project_or_404(pid)
    await require_role(pid, user)
    return {"ok": True, "sources": await store.list_sources(q, pid=pid)}

@router.post("/api/projects/{pid}/sources")
async def add_project_source(pid: str, body: Optional[dict] = None,
                             user: str = Depends(current_user)):
    await writable_project(pid, user)
    body = body or {}
    sid = body.get("source_id")
    if not sid or not await store.get_source(sid):
        raise HTTPException(404, "source not found")
    await projects.add_source(pid, sid, folder=body.get("folder") or "",
                              added_by=user)
    return {"ok": True, "sources": await store.list_sources(pid=pid)}

@router.delete("/api/projects/{pid}/sources/{sid}")
async def remove_project_source(pid: str, sid: str,
                                user: str = Depends(current_user)):
    await get_project_or_404(pid)
    await require_role(pid, user, "owner", "maintainer")
    if not await projects.remove_source(pid, sid):
        raise HTTPException(404, "source not in project")
    return {"ok": True, "sources": await store.list_sources(pid=pid)}

@router.post("/api/projects/{pid}/sources/{sid}/folder")
async def move_source(pid: str, sid: str, body: Optional[dict] = None,
                      user: str = Depends(current_user)):
    await writable_project(pid, user)
    body = body or {}
    if not await projects.set_source_folder(pid, sid, body.get("folder") or ""):
        raise HTTPException(404, "source not in project")
    return {"ok": True}

@router.get("/api/projects/{pid}/tree")
async def project_tree(pid: str, user: str = Depends(current_user)):
    p = await get_project_or_404(pid)
    await require_role(pid, user)
    links = {l["source_id"]: l for l in await projects.source_links(pid)}
    rows = []
    for sid, link in links.items():
        row = await store.get_source(sid)
        if not row:
            continue
        pub = store.to_public(row, await quiz_store.read_stats(pid, sid))
        pub["folder"] = link["folder"]
        pub["added_by"] = link["added_by"]
        rows.append(pub)
    return {"ok": True,
            "project": p,
            "folders": await projects.list_folders(pid),
            "sources": rows,
            "notes": await notes.list_standalone(pid),
            "colors": await projects.color_map(pid)}

@router.post("/api/projects/{pid}/folders")
async def create_folder(pid: str, body: Optional[dict] = None,
                        user: str = Depends(current_user)):
    await writable_project(pid, user)
    body = body or {}
    path = body.get("path") or ""
    new_path = body.get("new_path")
    try:
        out = (await projects.rename_folder(pid, path, new_path) if new_path
               else await projects.create_folder(pid, path))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "path": out, "folders": await projects.list_folders(pid)}

@router.delete("/api/projects/{pid}/folders")
async def delete_folder(pid: str, path: str = "",
                        user: str = Depends(current_user)):
    await writable_project(pid, user)
    try:
        await projects.delete_folder(pid, path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "folders": await projects.list_folders(pid)}

@router.get("/api/projects/{pid}/readme")
async def folder_readme(pid: str, folder: str = "",
                        user: str = Depends(current_user)):
    await get_project_or_404(pid)
    await require_role(pid, user)
    return {"ok": True, "note": await notes.readme_for_folder(pid, folder)}
