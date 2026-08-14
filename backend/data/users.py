"""User accounts.
The name is the identity the rest of the backend keys on: note authorship,
blame and project membership all store it as a plain string
"""

import time

from sqlalchemy import select

from ..core import db
from ..core.db import User
from . import projects


def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")

def norm(name: str) -> str:
    return (name or "").strip()[:120]

def _to_dict(u: User) -> dict:
    return {"name": u.name, "display_name": u.display_name or u.name,
            "created_at": u.created_at or ""}

async def get(name: str) -> dict | None:
    """The user, None when the name is blank or unknown
    """
    name = norm(name)
    if not name:
        return None
    async with db.session() as s:
        u = (await s.execute(select(User).where(User.name == name))).scalar_one_or_none()
    return _to_dict(u) if u else None

async def list_users() -> list:
    """Every user, oldest first
    """
    async with db.session() as s:
        us = (await s.execute(select(User).order_by(User.created_at))).scalars().all()
    return [_to_dict(u) for u in us]

async def create(name: str, display_name: str = "") -> dict:
    """Creates the user and their personal workspace, idempotently.
    An existing user is returned untouched, display_name included.
    Raises ValueError on a blank name
    """
    name = norm(name)
    if not name:
        raise ValueError("user name required")
    async with db.session() as s:
        exists = (await s.execute(select(User).where(User.name == name))).scalar_one_or_none()
        if exists is None:
            s.add(User(name=name, display_name=(display_name or name).strip()[:200],
                       created_at=_ts()))
            await s.commit()
    await projects.personal_project(name)  # every user has a workspace
    return await get(name)

async def ensure(name: str = "") -> dict:
    """The named user, created on the spot when missing.
    An empty name means the dev-mode default user
    """
    name = norm(name) or projects.DEFAULT_USER
    return await get(name) or await create(name)
