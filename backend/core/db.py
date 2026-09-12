"""The tables, and the one engine every request shares.
These models are the schema of record — ``migrations`` only catches an older
file up to them, it never leads.
Nothing is opened at import: the engine is built on the first ``session``,
and the file itself by ``init_db``
"""

import os

from sqlalchemy import Boolean, Index, Integer, String, Text, event
from sqlalchemy.ext.asyncio import (AsyncSession, async_sessionmaker,
                                    create_async_engine)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from . import config


DATA_DIR = config.DATA_DIR
DB_PATH = config.DB_PATH


class Base(DeclarativeBase):
    pass


class Source(Base):
    """A document of the library, shared by every project that adds it.
    The file lives under ``DATA_DIR`` at ``source_path``, and deleting the row
    is not enough to remove it
    """

    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # src_<hex>
    title: Mapped[str] = mapped_column(String(500), default="")
    source_type: Mapped[str] = mapped_column(String(20), default="")  # pdf|md|html|html+css
    source_path: Mapped[str] = mapped_column(String(500), default="")  # rel to DATA_DIR
    url: Mapped[str] = mapped_column(String(2000), default="")
    tags: Mapped[str] = mapped_column(String(500), default="")
    fetched_at: Mapped[str] = mapped_column(String(40), default="")


Index("ix_sources_url", Source.url)  # dedup lookups by import URL


class User(Base):
    """An author, identified by ``name`` alone.
    There is no credential here — authentication has not landed, and
    ``config.DEFAULT_USER`` stands in for whoever is at the keyboard
    """

    __tablename__ = "users"

    name: Mapped[str] = mapped_column(String(120), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[str] = mapped_column(String(40), default="")


class Project(Base):
    """A workspace gathering sources, note pages and members.
    ``kind`` is ``personal`` — one per user, named in ``owner_user``, and
    refused deletion — or ``team``, which leaves ``owner_user`` empty and
    knows its owner only through a member row.
    The four booleans are the project's capabilities, seeded at creation from
    ``config.PERSONAL_CAPS`` or ``TEAM_CAPS`` and editable per project after
    """

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # prj_<hex>
    name: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(String(40), default="")
    kind: Mapped[str] = mapped_column(String(20), default="team")  # personal|team
    owner_user: Mapped[str] = mapped_column(String(120), default="")  # kind=personal
    allow_quiz: Mapped[bool] = mapped_column(Boolean, default=False)
    multi_notes: Mapped[bool] = mapped_column(Boolean, default=True)
    show_blame: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_import: Mapped[bool] = mapped_column(Boolean, default=False)


class ProjectMember(Base):
    """One user's membership of one project, at one role.
    ``color`` is theirs throughout that project, in the blame gutter and under
    every anchor they made
    """

    __tablename__ = "project_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String(40), index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    role: Mapped[str] = mapped_column(String(20), default=config.DEFAULT_ROLE)
    color: Mapped[str] = mapped_column(String(20), default="")  # blame gutter


Index("ix_project_member_unique", ProjectMember.project_id, ProjectMember.name,
      unique=True)


class ProjectSource(Base):
    """A source placed in a project, at ``folder``.
    One source can sit in many projects, filed differently in each, which is
    why the folder belongs here and not on the source
    """

    __tablename__ = "project_sources"

    project_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    added_at: Mapped[str] = mapped_column(String(40), default="")
    folder: Mapped[str] = mapped_column(String(1000), default="")  # "a/b", "" = root
    added_by: Mapped[str] = mapped_column(String(120), default="")


class ProjectFolder(Base):
    """A folder of a project's library, ancestors stored as their own rows.
    The tree is really implied by ``ProjectSource.folder``, so these rows
    exist for the folders holding nothing yet
    """

    __tablename__ = "project_folders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String(40), index=True)
    path: Mapped[str] = mapped_column(String(1000), default="")
    created_at: Mapped[str] = mapped_column(String(40), default="")


Index("ix_project_folder_unique", ProjectFolder.project_id, ProjectFolder.path,
      unique=True)


class NotePage(Base):
    """One page of a project's notes, started by ``created_by`` but writable
    by anyone the project's roles allow.
    ``blame`` says who last wrote each line of ``content``, run-length encoded
    so a long page stays a short row, and ``version`` is bumped on every save
    to catch a stale editor
    """

    __tablename__ = "note_pages"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # note_<hex>
    project_id: Mapped[str] = mapped_column(String(40), index=True)
    source_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    folder: Mapped[str] = mapped_column(String(1000), default="")
    name: Mapped[str] = mapped_column(String(300), default="")
    position: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[str] = mapped_column(Text, default="")
    blame: Mapped[str] = mapped_column(Text, default="")  # JSON, run-length encoded
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[str] = mapped_column(String(40), default="")
    updated_at: Mapped[str] = mapped_column(String(40), default="")
    origin: Mapped[str] = mapped_column(Text, default="")  # JSON provenance


class NoteAnchor(Base):
    """A sentence of a note page tied to a passage of a source.
    Both ends are quote locators, JSON holding exact, prefix and suffix plus a
    character offset used only as a hint, so an anchor survives edits on
    either side and needs no marker inside the note text.
    Nothing here touches ``NotePage.content``
    """

    __tablename__ = "note_anchors"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # anc_<hex>
    project_id: Mapped[str] = mapped_column(String(40), index=True)
    note_id: Mapped[str] = mapped_column(String(40), index=True)
    source_id: Mapped[str] = mapped_column(String(40), default="")
    note_loc: Mapped[str] = mapped_column(Text, default="")  # JSON locator
    doc_loc: Mapped[str] = mapped_column(Text, default="")   # JSON locator
    created_by: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[str] = mapped_column(String(40), default="")


# Every anchor drawn over one document, for the multi-author overlay.
Index("ix_note_anchor_doc", NoteAnchor.project_id, NoteAnchor.source_id)


class SourceQuiz(Base):
    """The generated quiz for one source in one project, and the answers so
    far.
    Per project, so the same source quizzed in two of them keeps two scores
    """

    __tablename__ = "source_quiz"

    project_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    quiz: Mapped[str] = mapped_column(Text, default="")   # JSON
    stats: Mapped[str] = mapped_column(Text, default="")  # JSON


class BoardColumn(Base):
    """One column of a project's kanban board.
    Its cards hang off it, and deleting it takes them: a column without cards
    is a workflow concept, not a folder"""

    __tablename__ = "board_columns"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # col_<hex>
    project_id: Mapped[str] = mapped_column(String(40), index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    position: Mapped[int] = mapped_column(Integer, default=0)
    wip_limit: Mapped[int] = mapped_column(Integer, default=0)  # 0 = no limit
    color: Mapped[str] = mapped_column(String(20), default="")  # optional accent
    created_at: Mapped[str] = mapped_column(String(40), default="")


class BoardCard(Base):
    """One card of a kanban column.
    ``project_id`` is denormalized so a project sweep is one delete, and
    ``labels``/``assignees`` are comma-separated strings, the ``sources.tags``
    convention. ``source_id`` optionally points at a source of the same
    project; cards never own text lines, so there is no blame"""

    __tablename__ = "board_cards"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # crd_<hex>
    project_id: Mapped[str] = mapped_column(String(40), index=True)
    column_id: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    description: Mapped[str] = mapped_column(Text, default="")  # markdown
    labels: Mapped[str] = mapped_column(String(500), default="")
    assignees: Mapped[str] = mapped_column(String(500), default="")
    due_date: Mapped[str] = mapped_column(String(40), default="")  # YYYY-MM-DD
    position: Mapped[int] = mapped_column(Integer, default=0)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    source_id: Mapped[str] = mapped_column(String(40), default="")
    created_by: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[str] = mapped_column(String(40), default="")
    updated_at: Mapped[str] = mapped_column(String(40), default="")


Index("ix_board_column_project", BoardColumn.project_id)
Index("ix_board_card_column", BoardCard.column_id)
Index("ix_board_card_project", BoardCard.project_id)


_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _def_db() -> None:
    """Builds the engine and the session factory, once.
    The pragmas are per connection, not per database: WAL so a reader never
    blocks the writer, and a busy timeout so a concurrent write waits its turn
    instead of raising ``database is locked``
    """
    global _engine, _session_factory
    if _engine is not None:
        return
    _engine = create_async_engine(f"sqlite+aiosqlite:///{DB_PATH}", echo=False)

    @event.listens_for(_engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)

def session() -> AsyncSession:
    """A fresh session, building the engine on first use.
    Not a context manager itself — callers do ``async with db.session()``
    """
    _def_db()
    return _session_factory()

async def init_db() -> None:
    """Creates the database file, its directory, and every table still
    missing.
    Existing tables are left as they are, columns included, which is what
    ``migrations.run`` is for — so call this first
    """
    _def_db()
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    async with _engine.begin() as conn:  # type: ignore[union-attr]
        await conn.run_sync(Base.metadata.create_all)

async def dispose() -> None:
    """Closes the pool at shutdown.
    The engine is forgotten rather than marked dead, so a ``session`` after
    this quietly builds a new one instead of failing
    """
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
