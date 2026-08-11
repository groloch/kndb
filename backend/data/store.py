import json
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

def _loads(s: str | None, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default

def default_quiz(sid: str) -> dict:
    return {"source_id": sid, "updated_at": _ts(), "questions": []}

def new_stats(title: str, source_type: str, tags: str, category: str, url: str) -> dict:
    now = _ts()
    return {
        "title": title, "source_type": source_type, "tags": tags,
        "category": category, "url": url, "date_added": now,
        "date_last_modified": now, "num_questions": 0, "questions": {},
    }

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
        "notes": src.notes or "",
        "quiz": _loads(src.quiz, default_quiz(src.id)),
        "stats": _loads(src.stats, {}),
    }

def to_public(row: dict) -> dict:
    st = row["stats"] or {}
    return {
        "id": row["id"], "title": row["title"], "source_type": row["source_type"],
        "url": row["url"], "tags": row["tags"], "category": row["category"],
        "fetched_at": row["fetched_at"],
        "num_questions": st.get("num_questions", 0),
        "date_added": st.get("date_added", ""),
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

async def list_sources(q: str = "") -> list:
    tags, terms, projs = _parse_query(q)
    async with db.session() as s:
        rows = [_row_to_dict(x) for x in (await s.execute(select(Source))).scalars().all()]
    if projs:
        allowed = await projects.source_ids_for_project_terms(projs)
        rows = [r for r in rows if r["id"] in allowed]
    return [to_public(r) for r in rows if _matches(r, tags, terms)]

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
        notes=build_note_seed(title, source_type, url, seed),
        quiz=json.dumps(default_quiz(sid), ensure_ascii=False),
        stats=json.dumps(new_stats(title, source_type, tags, category, url),
                         ensure_ascii=False),
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
    if not quiz:
        quiz = json.dumps(default_quiz(id), ensure_ascii=False)
    if not stats:
        stats = json.dumps({}, ensure_ascii=False)
    src = Source(
        id=id, title=title, source_type=source_type, source_path=source_path,
        url=url, tags=tags, category=category, fetched_at=fetched_at,
        notes=notes or "", quiz=quiz, stats=stats,
    )
    async with db.session() as s:
        s.add(src)
        await s.commit()

async def update_meta(sid: str, title=None, tags=None, category=None) -> None:
    now = _ts()
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
        st = _loads(src.stats, {})
        if title is not None:
            st["title"] = title
        if tags is not None:
            st["tags"] = tags
        if category is not None:
            st["category"] = category
        st["date_last_modified"] = now
        src.stats = json.dumps(st, ensure_ascii=False)
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

async def save_note(sid: str, content: str) -> None:
    now = _ts()
    async with db.session() as s:
        src = (await s.execute(select(Source).where(Source.id == sid))).scalar_one_or_none()
        if src is None:
            raise KeyError(f"source {sid!r} not found")
        src.notes = content
        st = _loads(src.stats, {})
        st["date_last_modified"] = now
        src.stats = json.dumps(st, ensure_ascii=False)
        await s.commit()

async def read_quiz(sid: str) -> dict:
    row = await get_source(sid)
    return row["quiz"] if row else default_quiz(sid)

async def save_quiz(sid: str, quiz: dict) -> None:
    async with db.session() as s:
        src = (await s.execute(select(Source).where(Source.id == sid))).scalar_one_or_none()
        if src is None:
            raise KeyError(f"source {sid!r} not found")
        src.quiz = json.dumps(quiz, ensure_ascii=False)
        await s.commit()

async def read_stats(sid: str) -> dict:
    row = await get_source(sid)
    return row["stats"] if row else {}

async def save_stats(sid: str, stats: dict) -> None:
    async with db.session() as s:
        src = (await s.execute(select(Source).where(Source.id == sid))).scalar_one_or_none()
        if src is None:
            raise KeyError(f"source {sid!r} not found")
        src.stats = json.dumps(stats, ensure_ascii=False)
        await s.commit()
