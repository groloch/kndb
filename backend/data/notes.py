import json
import secrets
import time

from sqlalchemy import delete, select

from ..content import blame
from ..core import db
from ..core.db import NotePage
from . import projects


README = "README"
_EDIT_ANY = ("maintainer", "owner")


class NoteConflict(Exception):
    def __init__(self, version: int):
        super().__init__("this note was modified by someone else")
        self.version = version


class NotePermission(Exception):
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
    if not s:
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default

def _dumps(v) -> str:
    return json.dumps(v, ensure_ascii=False)

def _to_dict(p: NotePage, with_content: bool = True) -> dict:
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
    if role not in _EDIT_ANY:
        return f"deleting a note page needs maintainer or owner; you are {role}"
    others = [a for a in page.get("authors") or [] if a and a != user]
    if others:
        return (f"this page holds text written by {', '.join(others)} — only a "
                f"page written entirely by you can be deleted")
    return ""

async def get(nid: str) -> dict | None:
    async with db.session() as s:
        p = (await s.execute(select(NotePage).where(NotePage.id == nid))).scalar_one_or_none()
    return _to_dict(p) if p else None

async def list_for_source(pid: str, source_id: str) -> list:
    async with db.session() as s:
        rows = (await s.execute(
            select(NotePage).where(NotePage.project_id == pid,
                                   NotePage.source_id == source_id)
            .order_by(NotePage.position, NotePage.created_at))).scalars().all()
    return [_to_dict(p, with_content=False) for p in rows]

async def list_standalone(pid: str, folder: str | None = None) -> list:
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

async def ensure_single(pid: str, source_id: str, *, author: str,
                        seed_content: str = "") -> dict:
    pages = await list_for_source(pid, source_id)
    if pages:
        return await get(pages[0]["id"])
    return await create(pid, author=author, source_id=source_id,
                        content=seed_content)

async def save(nid: str, content: str, *, author: str, role: str,
               base_version: int | None = None) -> dict:
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

async def remove(nid: str) -> bool:
    async with db.session() as s:
        p = (await s.execute(select(NotePage).where(NotePage.id == nid))).scalar_one_or_none()
        if p is None:
            return False
        await s.delete(p)
        await s.commit()
    return True

async def delete_for_project(pid: str) -> None:
    async with db.session() as s:
        await s.execute(delete(NotePage).where(NotePage.project_id == pid))
        await s.commit()

async def delete_for_source(pid: str | None, source_id: str) -> None:
    stmt = delete(NotePage).where(NotePage.source_id == source_id)
    if pid is not None:
        stmt = stmt.where(NotePage.project_id == pid)
    async with db.session() as s:
        await s.execute(stmt)
        await s.commit()

async def move_folder(pid: str, path: str, new_path: str) -> None:
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
