import asyncio
import os
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from backend.api.deps import get_source_or_404
from backend.content import fetchers
from backend.data import store


router = APIRouter()

MAX_UPLOAD = 300 * 1024 * 1024


@router.get("/api/sources")
async def list_sources(q: str = ""):
    rows = await store.list_sources(q)
    return {"ok": True, "sources": rows}

@router.post("/api/import")
async def import_source(url: str = Form(""), file: Optional[UploadFile] = File(None)):
    url = (url or "").strip()
    if not url and file is None:
        raise HTTPException(400, "Provide a URL or a file to import")

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
        return {"ok": True, "id": sid, "title": title, "source_type": stype}

    kind = fetchers.classify_url(url)
    try:
        info = await fetchers.fetch_by_kind(kind, url)
    except Exception as e:
        raise HTTPException(502, f"Fetch failed: {e}")

    try:
        existing = await store.find_by_url(info.get("url") or url)
        if existing:
            return {
                "ok": True,
                "id": existing["id"],
                "title": existing["title"],
                "source_type": existing["source_type"],
                "duplicate": True,
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
    except Exception as e:
        raise HTTPException(500, f"Storing failed: {e}")
    return {"ok": True, "id": sid, "title": info["title"], "source_type": info["source_type"]}

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
    title = (body.get("title") or row["title"]).strip()
    tags = (body.get("tags") or "").strip()
    category = (body.get("category") or "").strip()
    await store.update_meta(sid, title=title, tags=tags, category=category)
    return {"ok": True, "id": sid, "title": title, "tags": tags, "category": category}

@router.delete("/api/source/{sid}")
async def source_delete(sid: str):
    if not await store.get_source(sid):
        raise HTTPException(404, f"source {sid} not found")
    await store.delete_source(sid)
    return {"ok": True}

@router.get("/api/note/{sid}")
async def get_note(sid: str):
    row = await get_source_or_404(sid)
    return {"ok": True, "content": row["notes"]}

@router.put("/api/note/{sid}")
async def put_note(sid: str, body: Optional[dict] = None):
    await get_source_or_404(sid)
    body = body or {}
    content = body.get("content")
    if content is None:
        raise HTTPException(400, "missing content")
    await store.save_note(sid, content)
    return {"ok": True}

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
