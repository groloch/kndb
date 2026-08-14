"""The source library: a metadata row per source, plus its blob on disk.
Row and file are written in separate steps, so nothing here is atomic — a
crash between the two leaves an empty file or a row pointing at one
"""

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

def text_sidecar(source_path: str) -> str:
    """Path of the cached plain-text rendition sitting next to a source blob.
    Present only when the fetcher had a cleaner text rendition than the blob
    itself, as arXiv HTML next to the PDF.
    No extension in TYPE_EXT is .txt, so this never collides with a real source
    """
    return os.path.splitext(source_path)[0] + ".txt"

def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")

def _row_to_dict(src: Source) -> dict:
    """Row as a dict, source_path made absolute.
    The column itself is relative to DATA_DIR, so the library can move
    """
    return {
        "id": src.id,
        "title": src.title,
        "source_type": src.source_type,
        "source_path": os.path.join(DATA_DIR, src.source_path),
        "url": src.url,
        "tags": src.tags,
        "fetched_at": src.fetched_at,
    }

def to_public(row: dict, stats: dict | None = None) -> dict:
    """The shape the API hands out: metadata, minus the path on disk.
    Quiz counts are per project, so without stats they read zero and
    date_added falls back to the fetch time
    """
    st = stats or {}
    return {
        "id": row["id"], "title": row["title"], "source_type": row["source_type"],
        "url": row["url"], "tags": row["tags"],
        "fetched_at": row["fetched_at"],
        "num_questions": st.get("num_questions", 0),
        "date_added": st.get("date_added", row["fetched_at"]),
    }

def _matches(row: dict, tags: list, terms: list) -> bool:
    """Whether a row carries every tag and every term, case-insensitively.
    Tags match whole, terms match anywhere in the title
    """
    rtags = {t.strip().lower() for t in (row.get("tags") or "").split(",") if t.strip()}
    if tags and not all(t in rtags for t in tags):
        return False
    title = (row.get("title") or "").lower()
    return all(t.lower() in title for t in terms)

def _parse_query(q: str):
    """Splits the search box into (tags, title terms, project names).
    @foo is a tag, /foo a project, anything else a term of the title
    """
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
    """The source, None when the id is unknown
    """
    async with db.session() as s:
        src = (await s.execute(select(Source).where(Source.id == sid))).scalar_one_or_none()
        return _row_to_dict(src) if src else None

async def find_by_url(url: str) -> dict | None:
    """The source imported from this URL, None when blank or never fetched.
    The dedup probe before importing again.
    ix_sources_url is not unique, so duplicates are possible and the earliest
    import wins rather than raising
    """
    if not url:
        return None
    async with db.session() as s:
        rows = await s.execute(
            select(Source).where(Source.url == url)
            .order_by(Source.fetched_at, Source.id)
        )
        src = rows.scalars().first()
        return _row_to_dict(src) if src else None

async def list_sources(q: str = "", pid: str = "") -> list:
    """Sources matching the query, in public shape, [] when none do.
    A pid narrows the list to that project's sources, and only then do the rows
    carry its quiz counts
    """
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
                        tags: str = "") -> tuple:
    """Creates the row and an empty blob file, returns (sid, abs_path).
    Writing the actual content into that path is the caller's job.
    Raises ValueError on a source type outside TYPE_EXT
    """
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
        url=url, tags=tags, fetched_at=now,
    )
    async with db.session() as s:
        s.add(src)
        await s.commit()
    return sid, abs_path

async def update_meta(sid: str, title=None, tags=None) -> None:
    """Renames or retags a source, a silent no-op when the id is unknown.
    A field left None keeps what is stored
    """
    async with db.session() as s:
        src = (await s.execute(select(Source).where(Source.id == sid))).scalar_one_or_none()
        if src is None:
            return
        if title is not None:
            src.title = title
        if tags is not None:
            src.tags = tags
        await s.commit()

async def delete_source(sid: str) -> None:
    """Drops the row, every project link, and both files on disk.
    Silent when the id is unknown, and a file that refuses to unlink is left
    behind rather than failing the delete
    """
    row = await get_source(sid)
    async with db.session() as s:
        src = (await s.execute(select(Source).where(Source.id == sid))).scalar_one_or_none()
        if src:
            await s.delete(src)
            await s.commit()
    await projects.remove_source_everywhere(sid)  # drop project links
    if not row:
        return
    for path in (row["source_path"], text_sidecar(row["source_path"])):
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
