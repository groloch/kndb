import json
import time

from sqlalchemy import select

from ..core import db
from ..core.db import SourceQuiz


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

def default_quiz(sid: str) -> dict:
    return {"source_id": sid, "updated_at": _ts(), "questions": []}

def new_stats(title: str = "", source_type: str = "", tags: str = "",
              url: str = "") -> dict:
    now = _ts()
    return {
        "title": title, "source_type": source_type, "tags": tags,
        "url": url, "date_added": now,
        "date_last_modified": now, "num_questions": 0, "questions": {},
    }

async def _row(pid: str, sid: str) -> SourceQuiz | None:
    async with db.session() as s:
        return (await s.execute(
            select(SourceQuiz).where(SourceQuiz.project_id == pid,
                                     SourceQuiz.source_id == sid))).scalar_one_or_none()

async def read_quiz(pid: str, sid: str) -> dict:
    row = await _row(pid, sid)
    return _loads(row.quiz, default_quiz(sid)) if row else default_quiz(sid)

async def read_stats(pid: str, sid: str) -> dict:
    row = await _row(pid, sid)
    return _loads(row.stats, {}) if row else {}

async def _upsert(pid: str, sid: str, *, quiz=None, stats=None) -> None:
    async with db.session() as s:
        row = (await s.execute(
            select(SourceQuiz).where(SourceQuiz.project_id == pid,
                                     SourceQuiz.source_id == sid))).scalar_one_or_none()
        if row is None:
            row = SourceQuiz(project_id=pid, source_id=sid, quiz="", stats="")
            s.add(row)
        if quiz is not None:
            row.quiz = _dumps(quiz)
        if stats is not None:
            row.stats = _dumps(stats)
        await s.commit()

async def save_quiz(pid: str, sid: str, quiz: dict) -> None:
    await _upsert(pid, sid, quiz=quiz)

async def save_stats(pid: str, sid: str, stats: dict) -> None:
    await _upsert(pid, sid, stats=stats)

async def touch(pid: str, sid: str) -> None:
    stats = await read_stats(pid, sid)
    if stats:
        stats["date_last_modified"] = _ts()
        await _upsert(pid, sid, stats=stats)
