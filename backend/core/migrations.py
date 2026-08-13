import json

from sqlalchemy import text

from . import db


_COLUMNS = {
    "projects": {
        "kind": "TEXT DEFAULT 'team'",
        "owner_user": "TEXT DEFAULT ''",
        "allow_quiz": "BOOLEAN DEFAULT 0",
        "multi_notes": "BOOLEAN DEFAULT 1",
        "show_blame": "BOOLEAN DEFAULT 1",
        "auto_import": "BOOLEAN DEFAULT 0",
    },
    "project_members": {"color": "TEXT DEFAULT ''"},
    "project_sources": {"folder": "TEXT DEFAULT ''", "added_by": "TEXT DEFAULT ''"},
}

# v1 kept the personal store on the source row, and a category that tags made
# redundant. Dropped only after ``_unify_workspace`` has moved their content out.
_DROPPED = {"sources": ("notes", "quiz", "stats", "category")}

_UNIFY = "unify_v2"


async def run() -> None:
    await _add_missing_columns()
    if not await _done(_UNIFY):
        await _unify_workspace()
        await _mark(_UNIFY)
    await _drop_legacy_columns()

async def _add_missing_columns() -> None:
    async with db.session() as s:
        for table, cols in _COLUMNS.items():
            rows = (await s.execute(text(f"PRAGMA table_info({table})"))).all()
            if not rows:
                continue  # table not created yet — the ORM will build it whole
            have = {r[1] for r in rows}
            for name, ddl in cols.items():
                if name not in have:
                    await s.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
        await s.commit()

async def _drop_legacy_columns() -> None:
    """Remove columns the models no longer declare.

    They are ``NOT NULL`` in every database the old models built, so leaving
    them would make the first insert after this release fail — the ORM stopped
    naming them. Runs after the v1 step, which is the one thing that still reads
    them."""
    async with db.session() as s:
        for table, cols in _DROPPED.items():
            have = {r[1] for r in
                    (await s.execute(text(f"PRAGMA table_info({table})"))).all()}
            for name in cols:
                if name in have:
                    await s.execute(text(f"ALTER TABLE {table} DROP COLUMN {name}"))
        await s.commit()

async def _done(name: str) -> bool:
    async with db.session() as s:
        await s.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_meta "
            "(key TEXT PRIMARY KEY, value TEXT)"))
        await s.commit()
        row = (await s.execute(
            text("SELECT value FROM schema_meta WHERE key = :k"),
            {"k": name})).first()
    return row is not None

async def _mark(name: str) -> None:
    async with db.session() as s:
        await s.execute(
            text("INSERT OR REPLACE INTO schema_meta (key, value) VALUES (:k, '1')"),
            {"k": name})
        await s.commit()

async def _unify_workspace() -> None:
    from ..data import notes, projects, users

    user = await users.ensure(projects.DEFAULT_USER)
    pid = await projects.personal_project_id(user["name"])

    rows = await _legacy_source_rows()
    async with db.session() as s:
        existing_projects = (await s.execute(text(
            "SELECT id FROM projects WHERE kind != 'personal' "
            "OR kind IS NULL"))).scalars().all()

    for sid, note, quiz, stats in rows:
        await projects.add_source(pid, sid, added_by=user["name"])
        if (note or "").strip():
            await notes.create(pid, author=user["name"], source_id=sid,
                               content=note)
        if (quiz or "").strip() or (stats or "").strip():
            await _restore_quiz(pid, sid, quiz, stats)

    for proj_id in existing_projects:
        await _assign_member_colors(proj_id)

async def _legacy_source_rows() -> list:
    """The notes, quiz and stats a v1 source row carried inline.

    A database created after the split has no such columns — there is nothing to
    carry over and the SELECT would simply fail, so ask SQLite what the table
    actually has before reading it."""
    async with db.session() as s:
        cols = {r[1] for r in
                (await s.execute(text("PRAGMA table_info(sources)"))).all()}
        if not {"notes", "quiz", "stats"} <= cols:
            return []
        return (await s.execute(text(
            "SELECT id, notes, quiz, stats FROM sources"))).all()

async def _restore_quiz(pid: str, sid: str, quiz: str, stats: str) -> None:
    from ..data import quiz as quiz_store

    def _load(raw, default):
        try:
            return json.loads(raw) if raw else default
        except (TypeError, ValueError):
            return default

    await quiz_store.save_quiz(pid, sid, _load(quiz, {}))
    await quiz_store.save_stats(pid, sid, _load(stats, {}))

async def _assign_member_colors(pid: str) -> None:
    from ..data.projects import PALETTE

    async with db.session() as s:
        rows = (await s.execute(text(
            "SELECT id, color FROM project_members WHERE project_id = :p"
            " ORDER BY id"), {"p": pid})).all()
        for i, (mid, color) in enumerate(rows):
            if not color:
                await s.execute(
                    text("UPDATE project_members SET color = :c WHERE id = :i"),
                    {"c": PALETTE[i % len(PALETTE)], "i": mid})
        await s.commit()
