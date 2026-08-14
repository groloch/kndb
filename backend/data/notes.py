"""Note pages: the per-author documents of a project.
A page hangs off a source or stands alone in a folder, and every line of it
carries its author in a run-length blame map
"""

import json
import secrets
import time

from sqlalchemy import delete, select

from ..content import blame
from ..core import config, db
from ..core.db import NotePage
from . import anchors, projects


README = "README"
_EDIT_ANY = config.EDIT_OTHERS_ROLES   # permissions.grants.edit_others in kndb.yaml


class NoteConflict(Exception):
    """The page moved on since the version the editor started from.
    Carries the version now stored, so the client can reload and rebase
    """
    def __init__(self, version: int):
        super().__init__("this note was modified by someone else")
        self.version = version


class NotePermission(Exception):
    """An edit reaching into lines somebody else wrote, without the role for it.
    Carries the offending line numbers, 1-based, and their owners
    """
    def __init__(self, lines: list, owners: list):
        who = ", ".join(sorted(set(owners)))
        super().__init__(
            f"only maintainers can edit notes written by others "
            f"({who}); lines {', '.join(str(n) for n in lines[:10])}"
            + (" …" if len(lines) > 10 else ""))
        self.lines = lines
        self.owners = sorted(set(owners))


def make_id() -> str:
    return "note_" + secrets.token_hex(6)

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

def _dumps(v) -> str:
    return json.dumps(v, ensure_ascii=False)

def _to_dict(p: NotePage, with_content: bool = True) -> dict:
    """Page as a dict, its authors read off the blame map.
    with_content=False drops the text and the blame, which is what the
    listings hand back
    """
    rle = _loads(p.blame, [])
    d = {
        "id": p.id,
        "project_id": p.project_id,
        "source_id": p.source_id or "",
        "folder": p.folder or "",
        "name": p.name or "",
        "position": p.position or 0,
        "version": p.version or 1,
        "created_by": p.created_by or "",
        "created_at": p.created_at or "",
        "updated_at": p.updated_at or "",
        "standalone": not (p.source_id or ""),
        "origin": _loads(p.origin, None),
        "authors": sorted({r.get("author") or "" for r in rle} - {""}),
    }
    if with_content:
        d["content"] = p.content or ""
        d["blame"] = rle
    return d

def deletable_by(page: dict, user: str, role: str) -> str:
    """Why this user cannot delete the page, "" when they can.
    Takes an edit-others role, and the page must hold nobody else's text
    """
    if role not in _EDIT_ANY:
        return (f"deleting a note page needs {' or '.join(_EDIT_ANY)}; "
                f"you are {role}")
    others = [a for a in page.get("authors") or [] if a and a != user]
    if others:
        return (f"this page holds text written by {', '.join(others)} — only a "
                f"page written entirely by you can be deleted")
    return ""

async def get(nid: str) -> dict | None:
    """The page with its content and blame, None when the id is unknown
    """
    async with db.session() as s:
        p = (await s.execute(select(NotePage).where(NotePage.id == nid))).scalar_one_or_none()
    return _to_dict(p) if p else None

async def list_for_source(pid: str, source_id: str) -> list:
    """The pages of one source, in page order, without their content
    """
    async with db.session() as s:
        rows = (await s.execute(
            select(NotePage).where(NotePage.project_id == pid,
                                   NotePage.source_id == source_id)
            .order_by(NotePage.position, NotePage.created_at))).scalars().all()
    return [_to_dict(p, with_content=False) for p in rows]

async def list_standalone(pid: str, folder: str | None = None) -> list:
    """The pages attached to no source, without their content.
    A folder of None spans the whole project, "" only the root
    """
    async with db.session() as s:
        stmt = select(NotePage).where(NotePage.project_id == pid,
                                      NotePage.source_id == "")
        if folder is not None:
            stmt = stmt.where(NotePage.folder == projects.norm_folder(folder))
        rows = (await s.execute(
            stmt.order_by(NotePage.folder, NotePage.position,
                          NotePage.name))).scalars().all()
    return [_to_dict(p, with_content=False) for p in rows]

async def combined_text(pid: str, source_id: str) -> str:
    """Every page of a source as one markdown document, for the generators.
    Blank pages are skipped, and a named page keeps its title and its author
    in a heading
    """
    async with db.session() as s:
        rows = (await s.execute(
            select(NotePage).where(NotePage.project_id == pid,
                                   NotePage.source_id == source_id)
            .order_by(NotePage.position, NotePage.created_at))).scalars().all()
    parts = []
    for p in rows:
        if not (p.content or "").strip():
            continue
        head = f"## {p.name} — @{p.created_by}\n\n" if (p.name or "").strip() else ""
        parts.append(head + p.content.strip())
    return "\n\n".join(parts)

async def readme_for_folder(pid: str, folder: str) -> dict | None:
    """The folder's README page, None when it has none.
    The name is matched whatever its case
    """
    folder = projects.norm_folder(folder)
    async with db.session() as s:
        rows = (await s.execute(
            select(NotePage).where(NotePage.project_id == pid,
                                   NotePage.source_id == "",
                                   NotePage.folder == folder))).scalars().all()
    for p in rows:
        if (p.name or "").strip().lower() == README.lower():
            return _to_dict(p)
    return None

async def create(pid: str, *, author: str, source_id: str = "", folder: str = "",
                 name: str = "", content: str = "", origin: dict | None = None,
                 rle: list | None = None) -> dict:
    """Creates a page and returns it, at version 1.
    It lands after the last page of the same source, and its lines are blamed
    on the author unless a blame map comes in with them.
    A page attached to a source is never in a folder
    """
    now = _ts()
    nid = make_id()
    source_id = source_id or ""
    async with db.session() as s:
        used = (await s.execute(
            select(NotePage.position).where(NotePage.project_id == pid,
                                            NotePage.source_id == source_id)
        )).scalars().all()
        page = NotePage(
            id=nid, project_id=pid, source_id=source_id,
            folder="" if source_id else projects.norm_folder(folder),
            name=(name or "").strip()[:300],
            position=(max(used) + 1) if used else 0,
            content=content or "",
            blame=_dumps(rle if rle is not None else blame.seed(content, author, now)),
            version=1, created_by=author, created_at=now, updated_at=now,
            origin=_dumps(origin) if origin else "",
        )
        s.add(page)
        await s.commit()
    return _to_dict(page)

async def ensure_single(pid: str, source_id: str, *, author: str) -> dict:
    """The source's first page, an empty one created when it has none
    """
    pages = await list_for_source(pid, source_id)
    if pages:
        return await get(pages[0]["id"])
    return await create(pid, author=author, source_id=source_id)

async def save(nid: str, content: str, *, author: str, role: str,
               base_version: int | None = None) -> dict:
    """Rewrites a page, rebases its blame, returns the saved page.
    Content that did not change is not a save: the version stays where it is.
    Raises KeyError on an unknown id, NoteConflict when base_version is behind
    the stored one (None skips the check), and NotePermission when the edit
    touches another author's lines without an edit-others role
    """
    now = _ts()
    async with db.session() as s:
        p = (await s.execute(select(NotePage).where(NotePage.id == nid))).scalar_one_or_none()
        if p is None:
            raise KeyError(f"note {nid!r} not found")
        if base_version is not None and int(base_version) != (p.version or 1):
            raise NoteConflict(p.version or 1)

        old, rle = p.content or "", _loads(p.blame, [])
        if content == old:
            return _to_dict(p)

        if role not in _EDIT_ANY:
            hit = blame.foreign_edits(old, content, rle, author)
            if hit:
                owners = [e["author"] for e in
                          blame.expand(rle, len(blame.split_lines(old)))]
                raise NotePermission(hit, [owners[i - 1] for i in hit])

        p.content = content
        p.blame = _dumps(blame.rebase(old, content, rle, author, now))
        p.version = (p.version or 1) + 1
        p.updated_at = now
        await s.commit()
    return _to_dict(p)

async def append(nid: str, text: str, *, author: str, rle: list | None = None,
                 header: str = "") -> dict:
    """Adds text at the end of a page, under an optional header.
    Trailing blank lines go first, so appended blocks stay one blank line
    apart.
    The new lines are blamed on the author unless a blame map comes with them,
    and an unknown id raises KeyError
    """
    now = _ts()
    mine = {"author": author, "ts": now}
    async with db.session() as s:
        p = (await s.execute(select(NotePage).where(NotePage.id == nid))).scalar_one_or_none()
        if p is None:
            raise KeyError(f"note {nid!r} not found")

        old = p.content or ""
        lines = blame.split_lines(old)
        entries = blame.expand(_loads(p.blame, []), len(lines))
        while lines and not lines[-1].strip():       # drop trailing blanks
            lines.pop()
            entries.pop()

        head = blame.split_lines(header) + [""] if header else []
        body = blame.split_lines(text.strip("\n"))
        body_entries = (blame.expand(rle, len(body)) if rle
                        else [mine] * len(body))

        sep = [""] if lines else []
        lines += sep + head + body
        entries += [mine] * (len(sep) + len(head)) + body_entries

        p.content = "\n".join(lines)
        p.blame = _dumps(blame.compress(entries))
        p.version = (p.version or 1) + 1
        p.updated_at = now
        await s.commit()
    return _to_dict(p)

async def rename(nid: str, *, name: str = None, folder: str = None,
                 position: int = None) -> dict | None:
    """Renames, refiles or reorders a page, None when the id is unknown.
    A field left None keeps what is stored, and a page attached to a source
    ignores folder.
    Returns the page without its content
    """
    async with db.session() as s:
        p = (await s.execute(select(NotePage).where(NotePage.id == nid))).scalar_one_or_none()
        if p is None:
            return None
        if name is not None:
            p.name = name.strip()[:300]
        if folder is not None and not (p.source_id or ""):
            p.folder = projects.norm_folder(folder)
        if position is not None:
            p.position = int(position)
        p.updated_at = _ts()
        await s.commit()
    return _to_dict(p, with_content=False)

# Anchors hang off a note page, so every path that drops pages drops theirs
# with them: deleting a page, unlinking a source, deleting a project.

async def remove(nid: str) -> bool:
    """Deletes a page and its anchors, False when the id is unknown
    """
    async with db.session() as s:
        p = (await s.execute(select(NotePage).where(NotePage.id == nid))).scalar_one_or_none()
        if p is None:
            return False
        await s.delete(p)
        await s.commit()
    await anchors.delete_for_note(nid)
    return True

async def delete_for_project(pid: str) -> None:
    """Drops every page of a project, anchors included
    """
    async with db.session() as s:
        await s.execute(delete(NotePage).where(NotePage.project_id == pid))
        await s.commit()
    await anchors.delete_for_project(pid)

async def delete_for_source(pid: str | None, source_id: str) -> None:
    """Drops the pages attached to a source, anchors included, pid None
    meaning every project at once
    """
    stmt = select(NotePage.id).where(NotePage.source_id == source_id)
    if pid is not None:
        stmt = stmt.where(NotePage.project_id == pid)
    async with db.session() as s:
        doomed = (await s.execute(stmt)).scalars().all()
        await s.execute(delete(NotePage).where(NotePage.id.in_(doomed)))
        await s.commit()
    await anchors.delete_for_notes(list(doomed))

async def move_folder(pid: str, path: str, new_path: str) -> None:
    """Moves the standalone pages of a folder subtree to a new path.
    Pages in a descendant folder follow, keeping their tail
    """
    async with db.session() as s:
        for p in (await s.execute(
            select(NotePage).where(NotePage.project_id == pid,
                                   NotePage.source_id == ""))).scalars():
            cur = p.folder or ""
            if cur == path:
                p.folder = new_path
            elif cur.startswith(path + "/"):
                p.folder = new_path + cur[len(path):]
        await s.commit()

async def lift_folder(pid: str, path: str, parent: str) -> None:
    """Empties a folder subtree into parent, for a folder being deleted.
    The pages survive the folder: they move up rather than go with it
    """
    async with db.session() as s:
        for p in (await s.execute(
            select(NotePage).where(NotePage.project_id == pid,
                                   NotePage.source_id == ""))).scalars():
            cur = p.folder or ""
            if cur == path:
                p.folder = parent
            elif cur.startswith(path + "/"):
                tail = cur[len(path) + 1:]
                p.folder = f"{parent}/{tail}" if parent else tail
        await s.commit()
