import os
import re
import secrets
import time

from sqlalchemy import func, select

from ..core import db
from ..core.db import Source
from . import projects


DATA_DIR = db.DATA_DIR
SOURCES_DIR = "sources"
TYPE_EXT = {"pdf": ".pdf", "md": ".md", "html": ".html", "html+css": ".html"}


def ensure_dirs() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, SOURCES_DIR), exist_ok=True)

def absdir(rel: str) -> str:
    return os.path.join(DATA_DIR, rel)

def make_id() -> str:
    return "src_" + secrets.token_hex(6)

def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")

def build_note_seed(title: str, source_type: str, url: str, seed: str = "") -> str:
    lines = [f"# {title}", ""]
    if url:
        lines += [f"- **source**: {url}", f"- **type**: {source_type}", ""]
    if seed.strip():
        lines += ["", "---", "", seed.strip(), ""]
    return "\n".join(lines)

def _row_to_dict(src: Source) -> dict:
    return {
        "id": src.id,
        "title": src.title,
        "source_type": src.source_type,
        "source_path": os.path.join(DATA_DIR, src.source_path),
        "url": src.url,
        "tags": src.tags,
        "category": src.category,
        "fetched_at": src.fetched_at,
    }

def to_public(row: dict, stats: dict | None = None) -> dict:
    st = stats or {}
    return {
        "id": row["id"], "title": row["title"], "source_type": row["source_type"],
        "url": row["url"], "tags": row["tags"], "category": row["category"],
        "fetched_at": row["fetched_at"],
        "num_questions": st.get("num_questions", 0),
        "date_added": st.get("date_added", row["fetched_at"]),
    }

def _matches(row: dict, tags: list, terms: list) -> bool:
    rtags = {t.strip().lower() for t in (row.get("tags") or "").split(",") if t.strip()}
    if tags and not all(t in rtags for t in tags):
        return False
    title = (row.get("title") or "").lower()
    return all(t.lower() in title for t in terms)

def _parse_query(q: str):
    q = (q or "").strip()
    if not q:
        return [], [], []
    tags, terms, projs = [], [], []
    for t in re.split(r"\s+", q):
        if t.startswith("@"):
            tags.append(t[1:].lower())
        elif t.startswith("/"):
            projs.append(t[1:].lower())
        elif t:
            terms.append(t)
    return tags, terms, projs

async def count_sources() -> int:
    async with db.session() as s:
        return (await s.execute(select(func.count()).select_from(Source))).scalar_one()

async def get_source(sid: str) -> dict | None:
    async with db.session() as s:
        src = (await s.execute(select(Source).where(Source.id == sid))).scalar_one_or_none()
        return _row_to_dict(src) if src else None

async def find_by_url(url: str) -> dict | None:
    if not url:
        return None
    async with db.session() as s:
        src = (await s.execute(select(Source).where(Source.url == url))).scalar_one_or_none()
        return _row_to_dict(src) if src else None

async def list_sources(q: str = "", pid: str = "") -> list:
    tags, terms, projs = _parse_query(q)
    async with db.session() as s:
        rows = [_row_to_dict(x) for x in (await s.execute(select(Source))).scalars().all()]
    if pid:
        allowed = {l["source_id"] for l in await projects.source_links(pid)}
        rows = [r for r in rows if r["id"] in allowed]
    if projs:
        allowed = await projects.source_ids_for_project_terms(projs)
        rows = [r for r in rows if r["id"] in allowed]
    hits = [r for r in rows if _matches(r, tags, terms)]
    if not pid:
        return [to_public(r) for r in hits]
    from . import quiz as quiz_store
    return [to_public(r, await quiz_store.read_stats(pid, r["id"])) for r in hits]

async def create_source(title: str, source_type: str, url: str = "",
                        tags: str = "", category: str = "", seed: str = "") -> tuple:
    """Create the DB row + empty source blob file. Returns ``(sid, abs_path)`` —
    the caller then writes the actual source content into the path."""
    ensure_dirs()
    if source_type not in TYPE_EXT:
        raise ValueError(f"unknown source type: {source_type}")
    sid = make_id()
    rel = os.path.join(SOURCES_DIR, sid + TYPE_EXT[source_type])
    abs_path = absdir(rel)
    now = _ts()
    open(abs_path, "wb").close()

    src = Source(
        id=sid, title=title, source_type=source_type, source_path=rel,
        url=url, tags=tags, category=category, fetched_at=now,
    )
    async with db.session() as s:
        s.add(src)
        await s.commit()
    return sid, abs_path

async def bulk_restore(*, id: str, title: str, source_type: str, source_path: str,
                       url: str = "", tags: str = "", category: str = "",
                       fetched_at: str = "", notes: str = "", quiz: str = "",
                       stats: str = "") -> None:
    """One-time migration helper: insert a fully-populated row verbatim."""
    if not fetched_at:
        fetched_at = _ts()
    src = Source(
        id=id, title=title, source_type=source_type, source_path=source_path,
        url=url, tags=tags, category=category, fetched_at=fetched_at,
        notes=notes or "", quiz=quiz or "", stats=stats or "",
    )
    async with db.session() as s:
        s.add(src)
        await s.commit()

async def update_meta(sid: str, title=None, tags=None, category=None) -> None:
    async with db.session() as s:
        src = (await s.execute(select(Source).where(Source.id == sid))).scalar_one_or_none()
        if src is None:
            return
        if title is not None:
            src.title = title
        if tags is not None:
            src.tags = tags
        if category is not None:
            src.category = category
        await s.commit()

async def delete_source(sid: str) -> None:
    row = await get_source(sid)
    async with db.session() as s:
        src = (await s.execute(select(Source).where(Source.id == sid))).scalar_one_or_none()
        if src:
            await s.delete(src)
            await s.commit()
    await projects.remove_source_everywhere(sid)  # drop project links
    if row and os.path.exists(row["source_path"]):
        try:
            os.remove(row["source_path"])
        except OSError:
            pass
