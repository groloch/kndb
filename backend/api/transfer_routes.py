import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from backend.api.deps import (current_user, get_note_or_404,
                              get_project_or_404, get_source_or_404,
                              require_role, writable_project)
from backend.content import blame
from backend.data import anchors, notes, projects


router = APIRouter()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")

def _provenance(project_name: str, page: dict, rle: list | None = None) -> str:
    bits = [f"from **{project_name}**"]
    if (page.get("name") or "").strip():
        bits.append(f'page "{page["name"]}"')
    who = _authors_by_weight(rle if rle is not None else page.get("blame"))
    if not who:
        who = [page.get("created_by", "")]
    bits.append(", ".join(f"@{a}" for a in who[:3] if a)
                + (" et al." if len(who) > 3 else ""))
    return "> " + " · ".join(b for b in bits if b)

def _authors_by_weight(rle: list | None) -> list:
    counts: dict = {}
    for run in rle or []:
        author = run.get("author") or ""
        if author:
            counts[author] = counts.get(author, 0) + int(run.get("n", 0))
    return sorted(counts, key=lambda a: -counts[a])

async def _land(target_pid: str, source_id: str, *, user: str, text: str,
                rle: list, header: str, name: str, origin: dict) -> dict:
    caps = await projects.get_capabilities(target_pid)

    if source_id and not await projects.has_source(target_pid, source_id):
        await projects.add_source(target_pid, source_id, added_by=user)

    if caps["multi_notes"] and source_id:
        if header:
            head = blame.split_lines(header) + [""]
            text = "\n".join(head + blame.split_lines(text))
            rle = blame.compress(
                [{"author": user, "ts": _now()}] * len(head)
                + blame.expand(rle, len(blame.split_lines(text)) - len(head)))
        return await notes.create(target_pid, author=user, source_id=source_id,
                                  name=name, content=text, rle=rle, origin=origin)

    if source_id:
        page = await notes.ensure_single(target_pid, source_id, author=user)
    else:
        page = await notes.create(target_pid, author=user, name=name or "Imported",
                                  folder="", content="", origin=origin)
    return await notes.append(page["id"], text, author=user, rle=rle, header=header)

@router.post("/api/transfer/source")
async def transfer_source(body: Optional[dict] = None,
                          user: str = Depends(current_user)):
    body = body or {}
    sid = (body.get("source_id") or "").strip()
    src_pid = (body.get("from") or "").strip()
    dst_pid = (body.get("to") or "").strip()
    if not sid or not dst_pid:
        raise HTTPException(400, "source_id and to are required")

    await get_source_or_404(sid)
    if src_pid:
        await get_project_or_404(src_pid)
        await require_role(src_pid, user)
        if not await projects.has_source(src_pid, sid):
            raise HTTPException(404, "that source is not in the origin project")
    await writable_project(dst_pid, user)

    added = await projects.add_source(dst_pid, sid, folder=body.get("folder") or "",
                                      added_by=user)
    if body.get("with_notes") and src_pid:
        for page in await notes.list_for_source(src_pid, sid):
            await _copy_page(page["id"], dst_pid, user)
    return {"ok": True, "added": added, "duplicate": not added}

async def _copy_page(nid: str, dst_pid: str, user: str) -> dict:
    page = await get_note_or_404(nid)
    origin_project = await get_project_or_404(page["project_id"])
    header = _provenance(origin_project["name"], page)
    landed = await _land(
        dst_pid, page["source_id"], user=user, text=page["content"],
        rle=page["blame"], header=header,
        name=page["name"] or origin_project["name"],
        origin={"project_id": page["project_id"], "note_id": page["id"],
                "at": page["updated_at"]})
    # The text arrived; its links to the document should arrive with it. Both
    # ends are quotes, so they resolve against the copy even when _land()
    # appended it under a header and moved every offset in the page.
    if page["source_id"]:
        await anchors.copy_to_note(nid, landed["id"], dst_pid)
    return landed

@router.post("/api/transfer/note")
async def transfer_note(body: Optional[dict] = None,
                        user: str = Depends(current_user)):
    body = body or {}
    nid = (body.get("note_id") or "").strip()
    dst_pid = (body.get("to") or "").strip()
    if not nid or not dst_pid:
        raise HTTPException(400, "note_id and to are required")

    page = await get_note_or_404(nid)
    await require_role(page["project_id"], user)   # must be able to read it
    await writable_project(dst_pid, user)
    if page["project_id"] == dst_pid:
        raise HTTPException(400, "that note is already in this project")

    return {"ok": True, "note": await _copy_page(nid, dst_pid, user)}

@router.post("/api/transfer/snippet")
async def transfer_snippet(body: Optional[dict] = None,
                           user: str = Depends(current_user)):
    body = body or {}
    nid = (body.get("note_id") or "").strip()
    dst_pid = (body.get("to") or "").strip()
    text = (body.get("text") or "").strip()
    if not nid or not dst_pid or not text:
        raise HTTPException(400, "note_id, to and text are required")

    page = await get_note_or_404(nid)
    await require_role(page["project_id"], user)
    await writable_project(dst_pid, user)
    origin_project = await get_project_or_404(page["project_id"])

    target_sid = (body.get("target_source_id") or page["source_id"] or "").strip()
    if target_sid:
        await get_source_or_404(target_sid)

    rle = _snippet_blame(page, text)
    landed = await _land(
        dst_pid, target_sid, user=user, text=text, rle=rle,
        header=_provenance(origin_project["name"], page, rle),
        name=page["name"] or origin_project["name"],
        origin={"project_id": page["project_id"], "note_id": page["id"],
                "snippet": True})
    return {"ok": True, "note": landed}

def _snippet_blame(page: dict, text: str) -> list:
    page_lines = blame.split_lines(page["content"])
    want = blame.split_lines(text)
    entries = blame.expand(page["blame"], len(page_lines))
    for i in range(0, len(page_lines) - len(want) + 1):
        if page_lines[i:i + len(want)] == want:
            return blame.compress(entries[i:i + len(want)])
    return []

@router.get("/api/transfer/targets")
async def transfer_targets(user: str = Depends(current_user)):
    personal = await projects.personal_project(user)
    teams = await projects.list_projects(user=user, kind="team")
    return {"ok": True, "personal": {"id": personal["id"], "name": personal["name"]},
            "projects": [{"id": p["id"], "name": p["name"],
                          "multi_notes": p["capabilities"]["multi_notes"]}
                         for p in teams]}

@router.get("/api/transfer/elsewhere/{sid}")
async def notes_elsewhere(sid: str, user: str = Depends(current_user)):
    await get_source_or_404(sid)
    mine = {p["id"] for p in await projects.list_projects(user=user, kind="")}
    out = []
    for pid in await projects.projects_holding_source(sid):
        proj = await projects.get_project(pid)
        if not proj or proj["kind"] == "personal":
            continue
        pages = await notes.list_for_source(pid, sid)
        if not pages:
            continue
        out.append({"project_id": pid, "name": proj["name"],
                    "note_count": len(pages), "member": pid in mine})
    return {"ok": True, "projects": out}

async def mirror_to_workspace(user: str, sid: str, source_pid: str) -> None:
    personal = await projects.personal_project(user)
    if not personal["capabilities"]["auto_import"]:
        return
    if personal["id"] == source_pid:
        return
    await projects.add_source(personal["id"], sid, added_by=user)
    await notes.ensure_single(personal["id"], sid, author=user)
