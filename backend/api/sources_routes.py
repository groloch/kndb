"""Getting documents in, and serving them back out.
A source is global — one blob, one row, however many projects link to it — so
importing the same URL twice files the existing one instead of copying it
"""

import asyncio
import os
from typing import Optional

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Request,
                     UploadFile)
from fastapi.responses import FileResponse

from backend.api.deps import (MANAGE, current_user, get_source_or_404,
                              require_role, writable_project)
from backend.api.transfer_routes import mirror_to_workspace
from backend.content import fetchers
from backend.core import config
from backend.data import notes, projects, store


router = APIRouter()

MAX_UPLOAD = config.UPLOAD_MAX_BYTES   # limits.upload_mb in kndb.yaml


@router.get("/api/sources")
async def list_sources(q: str = "", project: str = "",
                       user: str = Depends(current_user)):
    """The sources of one project, the caller's own workspace by default."""
    pid = project or await projects.personal_project_id(user)
    await require_role(pid, user)
    return {"ok": True, "sources": await store.list_sources(q, pid=pid)}

@router.post("/api/import")
async def import_source(request: Request, url: str = Form(""),
                        file: Optional[UploadFile] = File(None),
                        project: str = Form(""), folder: str = Form("")):
    """Files a document into a project, fetched from a URL or uploaded.
    Needs a writing role there. An upload may only be .pdf or .md, while a URL
    already in the library is filed again rather than fetched twice, and the
    answer says so
    """
    user = await current_user(request)
    url = (url or "").strip()
    if not url and file is None:
        raise HTTPException(400, "Provide a URL or a file to import")

    pid = project or await projects.personal_project_id(user)
    await writable_project(pid, user)

    if file is not None:
        try:
            size = file.size or 0
        except AttributeError:
            size = 0
        if size > MAX_UPLOAD:
            raise HTTPException(413, "file too large")
        name = os.path.basename(file.filename or "source")
        ext = os.path.splitext(name)[1].lower()
        if ext == ".pdf":
            stype = "pdf"
        elif ext in (".md", ".markdown"):
            stype = "md"
        else:
            raise HTTPException(400, "Only .pdf and .md/.markdown uploads are supported")
        title = os.path.splitext(name)[0]
        sid, spath = await store.create_source(title, stype, url="")
        await _write_upload(file, spath)
        await _file_into(pid, sid, user, folder)
        return {"ok": True, "id": sid, "title": title, "source_type": stype,
                "project_id": pid}

    kind = fetchers.classify_url(url)
    try:
        info = await fetchers.fetch_by_kind(kind, url)
    except Exception as e:
        raise HTTPException(502, f"Fetch failed: {e}")

    try:
        existing = await store.find_by_url(info.get("url") or url)
        if existing:
            added = await _file_into(pid, existing["id"], user, folder)
            return {
                "ok": True,
                "id": existing["id"],
                "title": existing["title"],
                "source_type": existing["source_type"],
                "duplicate": True,
                "already_here": not added,
                "project_id": pid,
            }
        sid, spath = await store.create_source(
            info["title"],
            info["source_type"],
            info.get("url") or url,
        )
        content = info["content"]
        if isinstance(content, str):
            await asyncio.to_thread(_write_text, spath, content)
        else:
            await asyncio.to_thread(_write_bytes, spath, content)
        if info.get("text"):
            await asyncio.to_thread(_write_text, store.text_sidecar(spath),
                                    info["text"])
        await _file_into(pid, sid, user, folder)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Storing failed: {e}")
    return {"ok": True, "id": sid, "title": info["title"],
            "source_type": info["source_type"], "project_id": pid}

async def _file_into(pid: str, sid: str, user: str, folder: str) -> bool:
    """Links a source into a project, false when it was already there.
    Opens its note page when the project keeps one per source, and mirrors the
    source into the caller's own workspace when they asked for that
    """
    added = await projects.add_source(pid, sid, folder=folder, added_by=user)
    if added:
        caps = await projects.get_capabilities(pid)
        if not caps["multi_notes"]:
            await notes.ensure_single(pid, sid, author=user)
    await mirror_to_workspace(user, sid, pid)
    return added

@router.get("/api/source/{sid}/content")
async def source_content(sid: str, user: str = Depends(current_user)):
    """The stored document, after verifying membership in a workspace holding it."""
    row = await get_source_or_404(sid)
    await _require_source_member(sid, user)
    mime = {
        "pdf": "application/pdf",
        "md": "text/markdown",
        "html": "text/html",
        "html+css": "text/html",
    }.get(row["source_type"], "application/octet-stream")
    return FileResponse(row["source_path"], media_type=mime)

@router.post("/api/source/{sid}/meta")
async def source_meta(sid: str, body: Optional[dict] = None,
                      user: str = Depends(current_user)):
    """Update globally shared metadata only when authorized in every workspace."""
    row = await get_source_or_404(sid)
    await _require_source_manager(sid, user)
    body = body or {}
    fields = {k: str(body[k] or "").strip()
              for k in ("title", "tags") if k in body}
    if fields.get("title") == "":
        del fields["title"]          # a source always keeps a title
    await store.update_meta(sid, **fields)
    return {"ok": True, "id": sid, **{k: fields.get(k, row[k])
                                      for k in ("title", "tags")}}

@router.delete("/api/source/{sid}")
async def source_delete(sid: str, user: str = Depends(current_user)):
    """Delete a source globally only when authorized in every workspace holding it."""
    if not await store.get_source(sid):
        raise HTTPException(404, f"source {sid} not found")
    await _require_source_manager(sid, user)
    await store.delete_source(sid)
    return {"ok": True}

async def _require_source_member(sid: str, user: str) -> None:
    pids = await projects.projects_holding_source(sid)
    member = False
    for pid in pids:
        if await projects.member_role(pid, user) is not None:
            member = True
            break
    if not member:
        raise HTTPException(403, "you are not a member of a workspace holding this source")


async def _require_source_manager(sid: str, user: str) -> None:
    pids = await projects.projects_holding_source(sid)
    if not pids:
        raise HTTPException(404, f"source {sid} is not linked to a workspace")
    # A personal workspace owner may alter the shared source only while the
    # source is not linked into someone else's workspace. Team links require
    # the caller to be a manager in every affected team.
    for pid in pids:
        role = await projects.member_role(pid, user)
        if role is None:
            raise HTTPException(403, "global source change affects a workspace you cannot manage")
        project = await projects.get_project(pid)
        if project["kind"] == "personal":
            if role != config.OWNER_ROLE:
                raise HTTPException(403, "personal workspace owner role required")
        elif role not in MANAGE:
            raise HTTPException(403, "global source change requires a managing role")


@router.get("/api/note/{sid}")
async def get_note(sid: str, user: str = Depends(current_user)):
    """The caller's own page on this source, created empty on first read.
    Always the one page of their personal workspace, never a project's
    """
    await get_source_or_404(sid)
    pid = await projects.personal_project_id(user)
    if not await projects.has_source(pid, sid):
        raise HTTPException(404, "source is not in your personal workspace")
    page = await notes.ensure_single(pid, sid, author=user)
    return {"ok": True, "content": page["content"], "note_id": page["id"],
            "version": page["version"], "blame": page["blame"]}

@router.put("/api/note/{sid}")
async def put_note(sid: str, body: Optional[dict] = None,
                   user: str = Depends(current_user)):
    """Saves that same page, 409 when base_version is behind what is stored.
    Written as the workspace owner, so the maintainer rule never bites: the
    only author there is the caller
    """
    await get_source_or_404(sid)
    body = body or {}
    content = body.get("content")
    if content is None:
        raise HTTPException(400, "missing content")
    pid = await projects.personal_project_id(user)
    if not await projects.has_source(pid, sid):
        raise HTTPException(404, "source is not in your personal workspace")
    page = await notes.ensure_single(pid, sid, author=user)
    try:
        saved = await notes.save(page["id"], content, author=user,
                                 role=projects.OWNER_ROLE,
                                 base_version=body.get("base_version"))
    except notes.NoteConflict as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True, "note_id": saved["id"], "version": saved["version"]}

def _write_text(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def _write_bytes(path: str, content: bytes) -> None:
    with open(path, "wb") as f:
        f.write(content)

async def _write_upload(file: UploadFile, path: str) -> None:
    """Streams an upload to disk a megabyte at a time, never holding it whole
    """
    with open(path, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)
