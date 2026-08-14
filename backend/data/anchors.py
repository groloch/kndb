"""Anchors: a sentence of a note page tied to a passage of a source.
An anchor is a row of its own — the note text is never marked up, so a page
reads the same whether or not anything is anchored to it
"""

import json
import secrets
import time

from sqlalchemy import delete, select

from ..core import config, db
from ..core.db import NoteAnchor


# A locator is stored as an opaque blob: resolving it needs the note text and
# the document's text layer, both of which live in the browser. The server
# only checks that it is the right shape and not unbounded.
#
# The document end is allowed more room because it also carries the rectangles
# the highlight is drawn from — one per line of the passage, so a long quote
# legitimately costs a few kilobytes. Both bounds exist to stop nonsense, not
# to police how much someone highlights — raise them under limits: in kndb.yaml.
MAX_LOC = config.ANCHOR_LOC_CHARS
MAX_DOC_LOC = config.ANCHOR_DOC_LOC_CHARS


class BadLocator(ValueError):
    """A locator that is not an object, quotes nothing, or is over its limit
    """
    pass


def make_id() -> str:
    return "anc_" + secrets.token_hex(6)

def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")

def _loads(s, default):
    """Parsed JSON, the default when the column is empty or corrupt
    """
    if not s:
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default

def _to_dict(a: NoteAnchor) -> dict:
    """Row as a dict, both locators parsed back into objects
    """
    return {
        "id": a.id,
        "project_id": a.project_id,
        "note_id": a.note_id,
        "source_id": a.source_id or "",
        "note_loc": _loads(a.note_loc, {}),
        "doc_loc": _loads(a.doc_loc, {}),
        "created_by": a.created_by or "",
        "created_at": a.created_at or "",
    }

def clean_note_loc(raw) -> str:
    """The note locator, validated, as the JSON blob to store.
    The note end always quotes text: there is nothing else to point at
    """
    return _clean(raw, "note_loc", quote=True, limit=MAX_LOC)

def clean_doc_loc(raw) -> str:
    """The document locator, validated, as the JSON blob to store.
    The document end quotes text too, except on a scanned page, where a
    dragged rectangle is all there is to record — such a region locator is
    rejected when it carries no rects
    """
    loc = raw if isinstance(raw, dict) else None
    kind = (loc or {}).get("kind") or "text"
    if kind == "region":
        if not (loc or {}).get("rects"):
            raise BadLocator("a region locator needs rects")
        return _clean(raw, "doc_loc", quote=False, limit=MAX_DOC_LOC)
    return _clean(raw, "doc_loc", quote=True, limit=MAX_DOC_LOC)

def _clean(raw, field: str, *, quote: bool, limit: int) -> str:
    """The locator as a compact JSON string, or BadLocator.
    Rejects anything that is not an object, a quote-less one when quote is
    set, and anything past limit characters once serialised
    """
    if not isinstance(raw, dict):
        raise BadLocator(f"{field} must be an object")
    if quote and not str(raw.get("exact") or "").strip():
        raise BadLocator(f"{field} must quote the text it points at")
    blob = json.dumps(raw, ensure_ascii=False)
    if len(blob) > limit:
        raise BadLocator(f"{field} is too large ({len(blob)} > {limit})")
    return blob

async def get(aid: str) -> dict | None:
    """The anchor, None when the id is unknown
    """
    async with db.session() as s:
        a = (await s.execute(
            select(NoteAnchor).where(NoteAnchor.id == aid))).scalar_one_or_none()
    return _to_dict(a) if a else None

async def create(pid: str, note_id: str, source_id: str, *, author: str,
                 note_loc, doc_loc) -> dict:
    """Records one link, returns it in public shape.
    Both locators are cleaned as the row is built, so a BadLocator leaves
    nothing written
    """
    now = _ts()
    a = NoteAnchor(
        id=make_id(), project_id=pid, note_id=note_id, source_id=source_id,
        note_loc=clean_note_loc(note_loc), doc_loc=clean_doc_loc(doc_loc),
        created_by=author, created_at=now,
    )
    async with db.session() as s:
        s.add(a)
        await s.commit()
    return _to_dict(a)

async def list_for_note(nid: str) -> list:
    """Every anchor of one note page, oldest first, [] when it has none
    """
    async with db.session() as s:
        rows = (await s.execute(
            select(NoteAnchor).where(NoteAnchor.note_id == nid)
            .order_by(NoteAnchor.created_at, NoteAnchor.id))).scalars().all()
    return [_to_dict(a) for a in rows]

async def list_for_source(pid: str, source_id: str) -> list:
    """Every anchor drawn over one document by anyone in the project, oldest
    first.
    What the reader overlay needs to show a teammate's passage alongside your
    own
    """
    async with db.session() as s:
        rows = (await s.execute(
            select(NoteAnchor).where(NoteAnchor.project_id == pid,
                                     NoteAnchor.source_id == source_id)
            .order_by(NoteAnchor.created_at, NoteAnchor.id))).scalars().all()
    return [_to_dict(a) for a in rows]

async def remove(aid: str) -> bool:
    """Deletes one anchor, False when the id is unknown
    """
    async with db.session() as s:
        a = (await s.execute(
            select(NoteAnchor).where(NoteAnchor.id == aid))).scalar_one_or_none()
        if a is None:
            return False
        await s.delete(a)
        await s.commit()
    return True

async def delete_for_note(nid: str) -> None:
    async with db.session() as s:
        await s.execute(delete(NoteAnchor).where(NoteAnchor.note_id == nid))
        await s.commit()

async def delete_for_notes(nids: list) -> None:
    if not nids:
        return
    async with db.session() as s:
        await s.execute(delete(NoteAnchor).where(NoteAnchor.note_id.in_(nids)))
        await s.commit()

async def delete_for_project_source(pid: str | None, source_id: str) -> None:
    """A document leaving a project takes the anchors drawn on it along, pid
    None meaning every project at once.
    Not the same sweep as dropping that source's note pages: a standalone page
    can quote a document it is not attached to, and those anchors die here too
    """
    stmt = delete(NoteAnchor).where(NoteAnchor.source_id == source_id)
    if pid is not None:
        stmt = stmt.where(NoteAnchor.project_id == pid)
    async with db.session() as s:
        await s.execute(stmt)
        await s.commit()

async def delete_for_project(pid: str) -> None:
    async with db.session() as s:
        await s.execute(delete(NoteAnchor).where(NoteAnchor.project_id == pid))
        await s.commit()

async def copy_to_note(src_nid: str, dst_nid: str, dst_pid: str) -> int:
    """Carries a page's anchors onto its copy, returns how many followed.
    Nothing is rewritten but the ids: source_id is global, and both locators
    are quotes, so they resolve against the copy — including when it was
    appended under a provenance header and every offset moved.
    Authorship is preserved, the way copied note text keeps its blame
    """
    rows = await list_for_note(src_nid)
    if not rows:
        return 0
    async with db.session() as s:
        for r in rows:
            s.add(NoteAnchor(
                id=make_id(), project_id=dst_pid, note_id=dst_nid,
                source_id=r["source_id"],
                note_loc=json.dumps(r["note_loc"], ensure_ascii=False),
                doc_loc=json.dumps(r["doc_loc"], ensure_ascii=False),
                created_by=r["created_by"], created_at=r["created_at"],
            ))
        await s.commit()
    return len(rows)
