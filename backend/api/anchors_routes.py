from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from backend.api.deps import (EDIT_OTHERS, WRITE, current_user,
                              get_note_or_404, get_project_or_404,
                              get_source_or_404, require_role)
from backend.data import anchors, projects


router = APIRouter()


# An anchor ties a sentence of a note page to a passage of a document. It is an
# annotation, never an edit: nothing here writes to a note's content, so the
# blame gutter and the maintainer rule are untouched by linking. That is also
# why creating one is allowed to anyone who could edit the page, including on
# lines somebody else wrote.


@router.get("/api/notes/{nid}/anchors")
async def list_note_anchors(nid: str, user: str = Depends(current_user)):
    page = await get_note_or_404(nid)
    await require_role(page["project_id"], user)
    return {"ok": True, "anchors": await anchors.list_for_note(nid)}

@router.get("/api/projects/{pid}/sources/{sid}/anchors")
async def list_source_anchors(pid: str, sid: str,
                              user: str = Depends(current_user)):
    await get_project_or_404(pid)
    await require_role(pid, user)
    return {"ok": True, "anchors": await anchors.list_for_source(pid, sid)}

@router.post("/api/notes/{nid}/anchors")
async def create_anchor(nid: str, body: Optional[dict] = None,
                        user: str = Depends(current_user)):
    body = body or {}
    page = await get_note_or_404(nid)
    pid = page["project_id"]
    await require_role(pid, user, *WRITE)

    sid = (body.get("source_id") or page["source_id"] or "").strip()
    if not sid:
        raise HTTPException(400, "source_id is required")
    await get_source_or_404(sid)
    # A standalone page may quote any document the project holds; a page
    # attached to a source may only quote that one, since anything else would
    # be a link the reader has no way to reach.
    if page["source_id"] and sid != page["source_id"]:
        raise HTTPException(400, "this page can only link to its own source")
    if not await projects.has_source(pid, sid):
        raise HTTPException(400, "that source is not in this project")

    try:
        anchor = await anchors.create(pid, nid, sid, author=user,
                                      note_loc=body.get("note_loc"),
                                      doc_loc=body.get("doc_loc"))
    except anchors.BadLocator as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "anchor": anchor}

@router.delete("/api/anchor/{aid}")
async def delete_anchor(aid: str, user: str = Depends(current_user)):
    anchor = await anchors.get(aid)
    if not anchor:
        raise HTTPException(404, f"anchor {aid} not found")
    role = await require_role(anchor["project_id"], user)
    if anchor["created_by"] != user and role not in EDIT_OTHERS:
        raise HTTPException(403, "only maintainers can remove someone else's link")
    await anchors.remove(aid)
    return {"ok": True}
