import secrets
import time

from sqlalchemy import delete, func, select

from ..core import db
from ..core.db import Project, ProjectMember, ProjectSource


CURRENT_USER = "me"
ROLES = {"owner", "maintainer", "contributor", "spectator"}
_DEFAULT_ROLE = "contributor"


def make_id() -> str:
    return "prj_" + secrets.token_hex(6)

def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")

async def _counts(pid: str):
    async with db.session() as s:
        members = (await s.execute(
            select(func.count()).select_from(ProjectMember)
            .where(ProjectMember.project_id == pid))).scalar_one()
        sources = (await s.execute(
            select(func.count()).select_from(ProjectSource)
            .where(ProjectSource.project_id == pid))).scalar_one()
    return members, sources

def _member_dict(m: ProjectMember) -> dict:
    return {"name": m.name, "role": m.role}

async def _project_dict(p: Project) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "completed": bool(p.completed),
        "created_at": p.created_at,
        "member_count": 0,
        "source_count": 0,
    }

async def list_projects(q: str = "") -> list:
    term = (q or "").strip().lower()
    async with db.session() as s:
        ps = (await s.execute(select(Project))).scalars().all()
    out = []
    for p in ps:
        if term and term not in p.name.lower() and term not in p.description.lower():
            continue
        d = await _project_dict(p)
        d["member_count"], d["source_count"] = await _counts(p.id)
        out.append(d)
    out.sort(key=lambda d: d["created_at"], reverse=True)
    return out

async def get_project(pid: str) -> dict | None:
    async with db.session() as s:
        p = (await s.execute(select(Project).where(Project.id == pid))).scalar_one_or_none()
        if p is None:
            return None
        members = (await s.execute(
            select(ProjectMember).where(ProjectMember.project_id == pid)
            .order_by(ProjectMember.role.desc()))).scalars().all()
        srcs = (await s.execute(
            select(ProjectSource.source_id).where(ProjectSource.project_id == pid)
        )).scalars().all()
    d = await _project_dict(p)
    d["member_count"], d["source_count"] = len(members), len(srcs)
    d["members"] = [_member_dict(m) for m in members]
    d["source_ids"] = list(srcs)
    return d

async def source_ids_for_project_terms(terms: list) -> set:
    if not terms:
        return set()
    async with db.session() as s:
        ps = (await s.execute(select(Project))).scalars().all()
    proj_ids = [p.id for p in ps
                if any(t in p.name.lower() for t in terms)]
    if not proj_ids:
        return set()
    async with db.session() as s:
        rows = (await s.execute(
            select(ProjectSource.source_id)
            .where(ProjectSource.project_id.in_(proj_ids)))).scalars().all()
    return set(rows)

async def create_project(name: str, description: str = "") -> dict:
    now = _ts()
    pid = make_id()
    p = Project(id=pid, name=name, description=description,
                completed=False, created_at=now)
    async with db.session() as s:
        s.add(p)
        s.add(ProjectMember(project_id=pid, name=CURRENT_USER, role="owner"))
        await s.commit()
    return await get_project(pid)

async def update_project(pid: str, name=None, description=None,
                         completed=None) -> dict | None:
    async with db.session() as s:
        p = (await s.execute(select(Project).where(Project.id == pid))).scalar_one_or_none()
        if p is None:
            return None
        if name is not None:
            p.name = name
        if description is not None:
            p.description = description
        if completed is not None:
            p.completed = bool(completed)
        await s.commit()
    return await get_project(pid)

async def delete_project(pid: str) -> None:
    async with db.session() as s:
        p = (await s.execute(select(Project).where(Project.id == pid))).scalar_one_or_none()
        if p:
            await s.delete(p)
        await s.execute(delete(ProjectMember).where(ProjectMember.project_id == pid))
        await s.execute(delete(ProjectSource).where(ProjectSource.project_id == pid))
        await s.commit()

async def list_members(pid: str) -> list:
    async with db.session() as s:
        ms = (await s.execute(
            select(ProjectMember).where(ProjectMember.project_id == pid)
            .order_by(ProjectMember.id))).scalars().all()
    return [_member_dict(m) for m in ms]

async def add_member(pid: str, name: str, role: str = _DEFAULT_ROLE) -> dict | None:
    role = role if role in ROLES else _DEFAULT_ROLE
    name = name.strip()[:120]
    if not name:
        raise ValueError("member name required")
    async with db.session() as s:
        exists = (await s.execute(
            select(ProjectMember).where(ProjectMember.project_id == pid,
                                        ProjectMember.name == name)
        )).scalar_one_or_none()
        if exists:
            return None  # duplicate
        s.add(ProjectMember(project_id=pid, name=name, role=role))
        await s.commit()
    return {"name": name, "role": role}

async def remove_member(pid: str, name: str) -> bool:
    async with db.session() as s:
        m = (await s.execute(
            select(ProjectMember).where(ProjectMember.project_id == pid,
                                        ProjectMember.name == name)
        )).scalar_one_or_none()
        if m is None:
            return False
        if m.role == "owner":
            owners = (await s.execute(
                select(func.count()).select_from(ProjectMember)
                .where(ProjectMember.project_id == pid,
                       ProjectMember.role == "owner"))).scalar_one()
            if owners <= 1:
                raise ValueError("cannot remove the last owner of a project")
        await s.delete(m)
        await s.commit()
    return True

async def set_member_role(pid: str, name: str, role: str) -> bool:
    role = role if role in ROLES else _DEFAULT_ROLE
    async with db.session() as s:
        m = (await s.execute(
            select(ProjectMember).where(ProjectMember.project_id == pid,
                                        ProjectMember.name == name)
        )).scalar_one_or_none()
        if m is None:
            return False
        if m.role == "owner" and role != "owner":
            owners = (await s.execute(
                select(func.count()).select_from(ProjectMember)
                .where(ProjectMember.project_id == pid,
                       ProjectMember.role == "owner"))).scalar_one()
            if owners <= 1:
                raise ValueError("cannot demote the last owner of a project")
        m.role = role
        await s.commit()
    return True

async def add_source(pid: str, source_id: str) -> bool:
    now = _ts()
    async with db.session() as s:
        exists = (await s.execute(
            select(ProjectSource).where(ProjectSource.project_id == pid,
                                        ProjectSource.source_id == source_id)
        )).scalar_one_or_none()
        if exists:
            return False
        s.add(ProjectSource(project_id=pid, source_id=source_id, added_at=now))
        await s.commit()
    return True

async def remove_source(pid: str, source_id: str) -> bool:
    async with db.session() as s:
        link = (await s.execute(
            select(ProjectSource).where(ProjectSource.project_id == pid,
                                        ProjectSource.source_id == source_id)
        )).scalar_one_or_none()
        if link is None:
            return False
        await s.delete(link)
        await s.commit()
    return True

async def remove_source_everywhere(source_id: str) -> None:
    """Personal source deletion must also drop its project links."""
    async with db.session() as s:
        await s.execute(delete(ProjectSource).where(ProjectSource.source_id == source_id))
        await s.commit()
