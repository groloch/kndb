"""Migration from the v1 (personal-first) schema.

Run in a subprocess with its own ``KNDB_CONFIG``: ``backend.core.config`` reads
its YAML once at import time, so a second database cannot be reached from a
process that has already imported it.

This is the test that protects existing installs — a v1 database holds the
user's real notes in ``sources.notes``.
"""

import json
import pathlib
import sqlite3
import subprocess
import sys
import textwrap

import pytest


ROOT = pathlib.Path(__file__).resolve().parent.parent

V1_SCHEMA = """
CREATE TABLE sources (
    id VARCHAR(40) NOT NULL PRIMARY KEY, title VARCHAR(500), source_type VARCHAR(20),
    source_path VARCHAR(500), url VARCHAR(2000), tags VARCHAR(500),
    category VARCHAR(500), fetched_at VARCHAR(40), notes TEXT, quiz TEXT, stats TEXT);
CREATE TABLE projects (
    id VARCHAR(40) NOT NULL PRIMARY KEY, name VARCHAR(200), description TEXT,
    completed BOOLEAN, created_at VARCHAR(40));
CREATE TABLE project_members (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, project_id VARCHAR(40),
    name VARCHAR(120), role VARCHAR(20));
CREATE TABLE project_sources (
    project_id VARCHAR(40) NOT NULL, source_id VARCHAR(40) NOT NULL,
    added_at VARCHAR(40), PRIMARY KEY (project_id, source_id));
"""

PROBE = textwrap.dedent("""
    import asyncio, json, sys
    from backend.core import db, migrations

    async def main():
        await db.init_db()
        await migrations.run()
        await migrations.run()          # idempotence: a second boot changes nothing
        async with db.session() as s:
            from sqlalchemy import text
            out = {}
            out["note_pages"] = [dict(r._mapping) for r in (await s.execute(text(
                "SELECT project_id, source_id, name, content, blame, created_by"
                " FROM note_pages"))).all()]
            out["quiz"] = [dict(r._mapping) for r in (await s.execute(text(
                "SELECT project_id, source_id, quiz, stats FROM source_quiz"))).all()]
            out["projects"] = [dict(r._mapping) for r in (await s.execute(text(
                "SELECT id, name, kind, owner_user, allow_quiz, multi_notes,"
                " show_blame, auto_import FROM projects"))).all()]
            out["members"] = [dict(r._mapping) for r in (await s.execute(text(
                "SELECT project_id, name, role, color FROM project_members"))).all()]
            out["links"] = [dict(r._mapping) for r in (await s.execute(text(
                "SELECT project_id, source_id, folder FROM project_sources"))).all()]
            out["source_columns"] = [r[1] for r in (await s.execute(text(
                "PRAGMA table_info(sources)"))).all()]
            out["tables"] = [r[0] for r in (await s.execute(text(
                "SELECT name FROM sqlite_master WHERE type = 'table'"))).all()]
        print("@@" + json.dumps(out))
        await db.dispose()

    asyncio.run(main())
""")


FRESH_PROBE = textwrap.dedent("""
    import asyncio, json
    from backend.core import db, migrations

    async def main():
        await db.init_db()
        await migrations.run()
        await migrations.run()
        async with db.session() as s:
            from sqlalchemy import text
            out = {}
            out["source_columns"] = [r[1] for r in (await s.execute(text(
                "PRAGMA table_info(sources)"))).all()]
            out["projects"] = [dict(r._mapping) for r in (await s.execute(text(
                "SELECT kind, owner_user FROM projects"))).all()]
        print("@@" + json.dumps(out))
        await db.dispose()

    asyncio.run(main())
""")


def _boot(data, probe):
    cfg = data / "kndb.yaml"
    cfg.write_text(f'data:\n  dir: "{str(data).replace(chr(92), "/")}"\n',
                   encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
        env={"KNDB_CONFIG": str(cfg), "PATH": "", "SYSTEMROOT": "C:\\Windows"},
    )
    assert proc.returncode == 0, proc.stderr
    payload = [l for l in proc.stdout.splitlines() if l.startswith("@@")]
    assert payload, proc.stdout + proc.stderr
    return json.loads(payload[0][2:])


@pytest.fixture(scope="module")
def fresh(tmp_path_factory):
    """A database built by the current models only — no v1 columns anywhere."""
    return _boot(tmp_path_factory.mktemp("fresh"), FRESH_PROBE)


def test_a_new_database_has_no_legacy_columns(fresh):
    assert not {"category", "notes", "quiz", "stats"} & set(fresh["source_columns"])


def test_a_new_database_boots_through_the_v1_migration(fresh):
    """The v1 step reads columns that no longer exist here; it has to notice
    rather than fail, and still leave the local user their workspace."""
    personal = [p for p in fresh["projects"] if p["kind"] == "personal"]
    assert [p["owner_user"] for p in personal] == ["me"]


@pytest.fixture(scope="module")
def migrated(tmp_path_factory):
    data = tmp_path_factory.mktemp("v1")
    db_path = data / "kndb.db"

    con = sqlite3.connect(db_path)
    con.executescript(V1_SCHEMA)
    con.execute(
        "INSERT INTO sources VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("src_aaa", "Attention Is All You Need", "pdf", "sources/src_aaa.pdf",
         "https://arxiv.org/abs/1706.03762", "ml,nlp", "papers",
         "2026-01-01T00:00:00", "# Notes\n\nTransformers are neat.",
         json.dumps({"source_id": "src_aaa", "questions": [{"id": "q1"}]}),
         json.dumps({"num_questions": 1, "date_added": "2026-01-01T00:00:00",
                     "questions": {"q1": {"times_played": 3}}})))
    con.execute(
        "INSERT INTO sources VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("src_bbb", "Empty one", "md", "sources/src_bbb.md", "", "", "",
         "2026-01-02T00:00:00", "", "", ""))
    con.execute("INSERT INTO projects VALUES (?,?,?,?,?)",
                ("prj_old", "Reading group", "legacy", 0, "2026-01-01T00:00:00"))
    con.execute("INSERT INTO project_members (project_id, name, role) VALUES (?,?,?)",
                ("prj_old", "me", "owner"))
    con.execute("INSERT INTO project_members (project_id, name, role) VALUES (?,?,?)",
                ("prj_old", "colleague", "contributor"))
    con.execute("INSERT INTO project_sources VALUES (?,?,?)",
                ("prj_old", "src_aaa", "2026-01-01T00:00:00"))
    con.commit()
    con.close()

    return _boot(data, PROBE)


def test_personal_workspace_is_created_for_the_local_user(migrated):
    personal = [p for p in migrated["projects"] if p["kind"] == "personal"]
    assert len(personal) == 1
    assert personal[0]["owner_user"] == "me"
    assert (personal[0]["allow_quiz"], personal[0]["multi_notes"]) == (1, 0)
    assert (personal[0]["show_blame"], personal[0]["auto_import"]) == (1, 1)


def test_existing_project_keeps_team_settings(migrated):
    old = next(p for p in migrated["projects"] if p["id"] == "prj_old")
    assert old["kind"] == "team"
    assert (old["allow_quiz"], old["multi_notes"]) == (0, 1)


def test_legacy_notes_become_a_note_page(migrated):
    personal = next(p["id"] for p in migrated["projects"] if p["kind"] == "personal")
    pages = [n for n in migrated["note_pages"] if n["source_id"] == "src_aaa"]
    assert len(pages) == 1
    assert pages[0]["project_id"] == personal
    assert "Transformers are neat." in pages[0]["content"]
    assert pages[0]["created_by"] == "me"


def test_migrated_notes_are_attributed(migrated):
    page = next(n for n in migrated["note_pages"] if n["source_id"] == "src_aaa")
    runs = json.loads(page["blame"])
    assert {r["author"] for r in runs} == {"me"}
    assert sum(r["n"] for r in runs) == len(page["content"].split("\n"))


def test_sources_without_notes_get_no_page(migrated):
    assert not [n for n in migrated["note_pages"] if n["source_id"] == "src_bbb"]


def test_quiz_and_stats_move_to_the_workspace(migrated):
    personal = next(p["id"] for p in migrated["projects"] if p["kind"] == "personal")
    row = next(q for q in migrated["quiz"] if q["source_id"] == "src_aaa")
    assert row["project_id"] == personal
    assert json.loads(row["quiz"])["questions"][0]["id"] == "q1"
    assert json.loads(row["stats"])["questions"]["q1"]["times_played"] == 3


def test_every_source_lands_in_the_workspace(migrated):
    personal = next(p["id"] for p in migrated["projects"] if p["kind"] == "personal")
    mine = {l["source_id"] for l in migrated["links"] if l["project_id"] == personal}
    assert mine == {"src_aaa", "src_bbb"}


def test_the_old_project_keeps_its_link(migrated):
    old = [l for l in migrated["links"] if l["project_id"] == "prj_old"]
    assert [l["source_id"] for l in old] == ["src_aaa"]


def test_existing_members_get_colors(migrated):
    old = [m for m in migrated["members"] if m["project_id"] == "prj_old"]
    colors = [m["color"] for m in old]
    assert all(c.startswith("#") for c in colors)
    assert len(set(colors)) == 2


def test_legacy_columns_are_dropped_once_their_content_has_moved(migrated):
    """They are NOT NULL and the models no longer name them, so a leftover
    column would break the next insert. The notes they held are asserted to have
    landed in ``note_pages`` above — this runs after that."""
    assert not {"category", "notes", "quiz", "stats"} & set(migrated["source_columns"])
    assert "title" in migrated["source_columns"]


def test_running_twice_does_not_duplicate(migrated):
    """The probe calls migrations.run() twice; one page proves the guard."""
    assert len([n for n in migrated["note_pages"] if n["source_id"] == "src_aaa"]) == 1


def test_an_existing_install_gains_the_anchor_table(migrated):
    """Anchors are a new table, not a new column, so create_all builds it on an
    old database without a hand-written migration step."""
    assert "note_anchors" in migrated["tables"]
