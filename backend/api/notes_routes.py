from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from backend.api.deps import (EDIT_OTHERS, WRITE, current_user,
                              get_note_or_404, get_project_or_404,
                              get_source_or_404, require_role,
                              writable_project)
from backend.data import notes, projects


router = APIRouter()


@router.get("/api/projects/{pid}/sources/{sid}/notes")
async def list_source_notes(pid: str, sid: str, user: str = Depends(current_user)):
    await get_project_or_404(pid)
    await require_role(pid, user)
    caps = await projects.get_capabilities(pid)
    pages = await notes.list_for_source(pid, sid)
    if not pages and not caps["multi_notes"]:
        from backend.data import store

        row = await get_source_or_404(sid)
        seed = store.build_note_seed(row["title"], row["source_type"], row["url"])
        await notes.ensure_single(pid, sid, author=user, seed_content=seed)
        pages = await notes.list_for_source(pid, sid)
    return {"ok": True, "notes": pages, "capabilities": caps,
            "colors": await projects.color_map(pid)}

@router.get("/api/projects/{pid}/notes")
async def list_standalone_notes(pid: str, folder: Optional[str] = None,
                                user: str = Depends(current_user)):
    await get_project_or_404(pid)
    await require_role(pid, user)
    return {"ok": True, "notes": await notes.list_standalone(pid, folder)}

@router.post("/api/projects/{pid}/notes")
async def create_note(pid: str, body: Optional[dict] = None,
                      user: str = Depends(current_user)):
    body = body or {}
    await writable_project(pid, user)
    caps = await projects.get_capabilities(pid)
    source_id = (body.get("source_id") or "").strip()

    if source_id:
        await get_source_or_404(source_id)
        if not await projects.has_source(pid, source_id):
            raise HTTPException(400, "that source is not in this project")
        if not caps["multi_notes"] and await notes.list_for_source(pid, source_id):
            raise HTTPException(
                409, "this workspace keeps a single note per source")
    name = (body.get("name") or "").strip()
    if not source_id and not name:
        raise HTTPException(400, "a standalone note needs a name")

    page = await notes.create(pid, author=user, source_id=source_id,
                              folder=body.get("folder") or "", name=name,
                              content=body.get("content") or "")
    return {"ok": True, "note": page}

@router.get("/api/notes/{nid}")
async def read_note(nid: str, user: str = Depends(current_user)):
    page = await get_note_or_404(nid)
    role = await require_role(page["project_id"], user)
    caps = await projects.get_capabilities(page["project_id"])
    return {"ok": True, "note": page, "role": role,
            "can_edit": role in WRITE,
            "can_edit_others": role in EDIT_OTHERS,
            "can_delete": not notes.deletable_by(page, user, role),
            "show_blame": caps["show_blame"],
            "colors": await projects.color_map(page["project_id"])}

@router.put("/api/notes/{nid}")
async def save_note(nid: str, body: Optional[dict] = None,
                    user: str = Depends(current_user)):
    body = body or {}
    page = await get_note_or_404(nid)
    role = await require_role(page["project_id"], user, *WRITE)
    content = body.get("content")
    if content is None:
        raise HTTPException(400, "missing content")
    try:
        saved = await notes.save(nid, content, author=user, role=role,
                                 base_version=body.get("base_version"))
    except notes.NoteConflict as e:
        raise HTTPException(409, str(e)) from e
    except notes.NotePermission as e:
        raise HTTPException(403, str(e)) from e
    return {"ok": True, "note": saved}

@router.patch("/api/notes/{nid}")
async def patch_note(nid: str, body: Optional[dict] = None,
                     user: str = Depends(current_user)):
    body = body or {}
    page = await get_note_or_404(nid)
    role = await require_role(page["project_id"], user, *WRITE)
    if page["created_by"] != user and role not in EDIT_OTHERS:
        raise HTTPException(403, "only maintainers can rename someone else's note")
    out = await notes.rename(nid, name=body.get("name"), folder=body.get("folder"),
                             position=body.get("position"))
    return {"ok": True, "note": out}

@router.delete("/api/notes/{nid}")
async def delete_note(nid: str, user: str = Depends(current_user)):
    page = await get_note_or_404(nid)
    role = await require_role(page["project_id"], user)
    why = notes.deletable_by(page, user, role)
    if why:
        raise HTTPException(403, why)
    caps = await projects.get_capabilities(page["project_id"])
    if not caps["multi_notes"] and page["source_id"]:
        raise HTTPException(409, "this workspace keeps a single note per source")
    await notes.remove(nid)
    return {"ok": True}
