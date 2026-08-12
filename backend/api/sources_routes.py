import asyncio
import os
from typing import Optional

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Request,
                     UploadFile)
from fastapi.responses import FileResponse

from backend.api.deps import current_user, get_source_or_404, writable_project
from backend.api.transfer_routes import mirror_to_workspace
from backend.content import fetchers
from backend.data import notes, projects, store


router = APIRouter()

MAX_UPLOAD = 300 * 1024 * 1024


@router.get("/api/sources")
async def list_sources(q: str = "", project: str = "",
                       user: str = Depends(current_user)):
    pid = project or await projects.personal_project_id(user)
    return {"ok": True, "sources": await store.list_sources(q, pid=pid)}

@router.post("/api/import")
async def import_source(request: Request, url: str = Form(""),
                        file: Optional[UploadFile] = File(None),
                        project: str = Form(""), folder: str = Form("")):
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
            added = await _file_into(pid, existing["id"], user, folder,
                                     seed=info.get("seed", ""))
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
            seed=info.get("seed", ""),
        )
        content = info["content"]
        if isinstance(content, str):
            await asyncio.to_thread(_write_text, spath, content)
        else:
            await asyncio.to_thread(_write_bytes, spath, content)
        await _file_into(pid, sid, user, folder, seed=info.get("seed", ""))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Storing failed: {e}")
    return {"ok": True, "id": sid, "title": info["title"],
            "source_type": info["source_type"], "project_id": pid}

async def _file_into(pid: str, sid: str, user: str, folder: str,
                     seed: str = "") -> bool:
    added = await projects.add_source(pid, sid, folder=folder, added_by=user)
    if added:
        caps = await projects.get_capabilities(pid)
        row = await store.get_source(sid)
        if row and not caps["multi_notes"]:
            await notes.ensure_single(
                pid, sid, author=user,
                seed_content=store.build_note_seed(
                    row["title"], row["source_type"], row["url"], seed))
    await mirror_to_workspace(user, sid, pid)
    return added

@router.get("/api/source/{sid}/content")
async def source_content(sid: str):
    row = await get_source_or_404(sid)
    mime = {
        "pdf": "application/pdf",
        "md": "text/markdown",
        "html": "text/html",
        "html+css": "text/html",
    }.get(row["source_type"], "application/octet-stream")
    return FileResponse(row["source_path"], media_type=mime)

@router.post("/api/source/{sid}/meta")
async def source_meta(sid: str, body: Optional[dict] = None):
    row = await get_source_or_404(sid)
    body = body or {}
    fields = {k: str(body[k] or "").strip()
              for k in ("title", "tags", "category") if k in body}
    if fields.get("title") == "":
        del fields["title"]          # a source always keeps a title
    await store.update_meta(sid, **fields)
    return {"ok": True, "id": sid, **{k: fields.get(k, row[k])
                                      for k in ("title", "tags", "category")}}

@router.delete("/api/source/{sid}")
async def source_delete(sid: str):
    if not await store.get_source(sid):
        raise HTTPException(404, f"source {sid} not found")
    await store.delete_source(sid)
    return {"ok": True}

@router.get("/api/note/{sid}")
async def get_note(sid: str, user: str = Depends(current_user)):
    row = await get_source_or_404(sid)
    pid = await projects.personal_project_id(user)
    page = await notes.ensure_single(
        pid, sid, author=user,
        seed_content=store.build_note_seed(row["title"], row["source_type"],
                                           row["url"]))
    return {"ok": True, "content": page["content"], "note_id": page["id"],
            "version": page["version"], "blame": page["blame"]}

@router.put("/api/note/{sid}")
async def put_note(sid: str, body: Optional[dict] = None,
                   user: str = Depends(current_user)):
    await get_source_or_404(sid)
    body = body or {}
    content = body.get("content")
    if content is None:
        raise HTTPException(400, "missing content")
    pid = await projects.personal_project_id(user)
    page = await notes.ensure_single(pid, sid, author=user)
    try:
        saved = await notes.save(page["id"], content, author=user, role="owner",
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
    with open(path, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)
