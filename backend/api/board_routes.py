"""The kanban board of one project: columns, cards and their order.
Reading needs membership, everything else a writing role — cards own no text
lines, so there is no line-level permission between writers. A project marked
completed is read-only like the rest of the app
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from backend.api.deps import (current_user, get_project_or_404, require_role,
                              writable_project)
from backend.data import board, projects


router = APIRouter()


async def _writable_board(pid: str, user: str):
    """The project, once the caller is allowed to write in it and it is not
    archived as completed
    """
    p = await writable_project(pid, user)
    if p["completed"]:
        raise HTTPException(403, "this project is completed and read-only")
    return p

async def _check_assignees(pid: str, assignees) -> None:
    """Refuses a list naming anyone who is not a project member
    """
    if assignees is None:
        return
    member_names = {m["name"] for m in await projects.list_members(pid)}
    bad = [a for a in assignees if a not in member_names]
    if bad:
        raise HTTPException(400, f"not a member of this project: {', '.join(bad)}")

async def _check_source(pid: str, source_id) -> None:
    """Refuses a card-to-source link naming a source the project does not hold
    """
    if source_id in (None, ""):
        return
    if not await projects.has_source(pid, source_id):
        raise HTTPException(400, "that source is not in this project")


@router.get("/api/projects/{pid}/board")
async def get_board(pid: str, user: str = Depends(current_user)):
    """Everything the Board tab draws, to a member
    """
    await get_project_or_404(pid)
    await require_role(pid, user)
    return {"ok": True, "board": await board.get_board(pid)}

@router.post("/api/projects/{pid}/board/columns")
async def add_column(pid: str, body: Optional[dict] = None,
                     user: str = Depends(current_user)):
    """The new column, appended at the end, to a writer
    """
    await _writable_board(pid, user)
    body = body or {}
    try:
        await board.create_column(pid, body.get("name") or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "board": await board.get_board(pid)}

@router.patch("/api/projects/{pid}/board/columns/{cid}")
async def patch_column(pid: str, cid: str, body: Optional[dict] = None,
                       user: str = Depends(current_user)):
    """The column after a rename, WIP limit, colour or move, to a writer.
    Only the keys the body carries change
    """
    await _writable_board(pid, user)
    body = body or {}
    try:
        col = await board.update_column(
            pid, cid, name=body.get("name"), wip_limit=body.get("wip_limit"),
            color=body.get("color"), position=body.get("position"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if col is None:
        raise HTTPException(404, f"column {cid} not found")
    return {"ok": True, "board": await board.get_board(pid)}

@router.delete("/api/projects/{pid}/board/columns/{cid}")
async def remove_column(pid: str, cid: str, user: str = Depends(current_user)):
    """Deletes a column and its cards, to a writer.
    409 on the project's last column, which cannot go
    """
    await _writable_board(pid, user)
    try:
        ok = await board.delete_column(pid, cid)
    except ValueError as e:
        raise HTTPException(409, str(e))
    if not ok:
        raise HTTPException(404, f"column {cid} not found")
    return {"ok": True, "board": await board.get_board(pid)}

@router.post("/api/projects/{pid}/board/columns/order")
async def order_columns(pid: str, body: Optional[dict] = None,
                        user: str = Depends(current_user)):
    """The column order after a drag-reorder, to a writer.
    The body names every column, in the order it should end up in
    """
    await _writable_board(pid, user)
    body = body or {}
    try:
        await board.reorder_columns(pid, body.get("column_ids") or [])
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "board": await board.get_board(pid)}

@router.post("/api/projects/{pid}/board/cards")
async def add_card(pid: str, body: Optional[dict] = None,
                   user: str = Depends(current_user)):
    """The new card at the bottom of a column, to a writer.
    The assignees must be members and the linked source one of the project's
    """
    await _writable_board(pid, user)
    body = body or {}
    await _check_assignees(pid, body.get("assignees"))
    await _check_source(pid, body.get("source_id"))
    try:
        await board.create_card(
            pid, body.get("column_id") or "", title=body.get("title") or "",
            description=body.get("description") or "",
            labels=body.get("labels") or (), assignees=body.get("assignees") or (),
            due_date=body.get("due_date") or "", source_id=body.get("source_id") or "",
            author=user)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "board": await board.get_board(pid)}

@router.patch("/api/projects/{pid}/board/cards/{card_id}")
async def patch_card(pid: str, card_id: str, body: Optional[dict] = None,
                     user: str = Depends(current_user)):
    """The card after a partial change, to a writer.
    Archiving is done here too: ``archived: true`` hides it from the flow
    """
    await _writable_board(pid, user)
    body = body or {}
    await _check_assignees(pid, body.get("assignees"))
    await _check_source(pid, body.get("source_id"))
    try:
        card = await board.update_card(
            pid, card_id, title=body.get("title"), description=body.get("description"),
            labels=body.get("labels"), assignees=body.get("assignees"),
            due_date=body.get("due_date"), source_id=body.get("source_id"),
            archived=body.get("archived"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if card is None:
        raise HTTPException(404, f"card {card_id} not found")
    return {"ok": True, "board": await board.get_board(pid)}

@router.delete("/api/projects/{pid}/board/cards/{card_id}")
async def remove_card(pid: str, card_id: str, user: str = Depends(current_user)):
    """Permanently deletes a card, to a writer.
    Archiving is the soft path; this is the destructive one
    """
    await _writable_board(pid, user)
    if not await board.delete_card(pid, card_id):
        raise HTTPException(404, f"card {card_id} not found")
    return {"ok": True, "board": await board.get_board(pid)}

@router.post("/api/projects/{pid}/board/move")
async def move_card(pid: str, body: Optional[dict] = None,
                    user: str = Depends(current_user)):
    """The card after a move to another column or position, to a writer.
    What a drag & drop saves
    """
    await _writable_board(pid, user)
    body = body or {}
    try:
        card = await board.move_card(
            pid, body.get("card_id") or "", column_id=body.get("column_id"),
            position=body.get("position"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if card is None:
        raise HTTPException(404, f"card {body.get('card_id') or ''} not found")
    return {"ok": True, "board": await board.get_board(pid)}