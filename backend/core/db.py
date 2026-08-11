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
    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # src_<hex>
    title: Mapped[str] = mapped_column(String(500), default="")
    source_type: Mapped[str] = mapped_column(String(20), default="")  # pdf|md|html|html+css
    source_path: Mapped[str] = mapped_column(String(500), default="")  # rel to DATA_DIR
    url: Mapped[str] = mapped_column(String(2000), default="")
    tags: Mapped[str] = mapped_column(String(500), default="")
    category: Mapped[str] = mapped_column(String(500), default="")
    fetched_at: Mapped[str] = mapped_column(String(40), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    quiz: Mapped[str] = mapped_column(Text, default="")   # JSON
    stats: Mapped[str] = mapped_column(Text, default="")  # JSON


Index("ix_sources_url", Source.url)  # dedup lookups by import URL


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # prj_<hex>
    name: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(String(40), default="")


class ProjectMember(Base):
    __tablename__ = "project_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String(40), index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    role: Mapped[str] = mapped_column(String(20), default="contributor")


Index("ix_project_member_unique", ProjectMember.project_id, ProjectMember.name,
      unique=True)


class ProjectSource(Base):
    __tablename__ = "project_sources"

    project_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    added_at: Mapped[str] = mapped_column(String(40), default="")

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _def_db() -> None:
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
    _def_db()
    return _session_factory()

async def init_db() -> None:
    _def_db()
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    async with _engine.begin() as conn:  # type: ignore[union-attr]
        await conn.run_sync(Base.metadata.create_all)

async def dispose() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
