import secrets
import time

from sqlalchemy import delete, func, select

from ..core import config, db
from ..core.db import (Project, ProjectFolder, ProjectMember, ProjectSource,
                       SourceQuiz)


# All of these are knobs in kndb.yaml; re-exported here because this module is
# where the rest of the backend reaches for them.
DEFAULT_USER = config.DEFAULT_USER   # dev-mode fallback until real auth lands
ROLES = set(config.ROLES)
DEFAULT_ROLE = config.DEFAULT_ROLE
OWNER_ROLE = config.OWNER_ROLE

PALETTE = list(config.MEMBER_COLORS)
EXTERNAL_COLOR = config.EXTERNAL_COLOR

PERSONAL_CAPS = config.PERSONAL_CAPS
TEAM_CAPS = config.TEAM_CAPS

_CAP_KEYS = config.CAP_KEYS


def make_id() -> str:
    return "prj_" + secrets.token_hex(6)

def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")

def norm_folder(path: str) -> str:
    parts = [p.strip() for p in (path or "").replace("\\", "/").split("/")]
    return "/".join(p for p in parts if p and p not in (".", ".."))[:1000]

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
    return {"name": m.name, "role": m.role, "color": m.color or EXTERNAL_COLOR}

def _project_dict(p: Project) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "completed": bool(p.completed),
        "created_at": p.created_at,
        "kind": p.kind or "team",
        "owner_user": p.owner_user or "",
        "capabilities": {k: bool(getattr(p, k)) for k in _CAP_KEYS},
        "member_count": 0,
        "source_count": 0,
    }

async def list_projects(q: str = "", user: str = "", kind: str = "team") -> list:
    term = (q or "").strip().lower()
    async with db.session() as s:
        stmt = select(Project)
        if kind:
            stmt = stmt.where(Project.kind == kind)
        ps = (await s.execute(stmt)).scalars().all()
        mine = None
        if user:
            mine = set((await s.execute(
                select(ProjectMember.project_id)
                .where(ProjectMember.name == user))).scalars().all())
    out = []
    for p in ps:
        if mine is not None and p.id not in mine:
            continue
        if term and term not in p.name.lower() and term not in (p.description or "").lower():
            continue
        d = _project_dict(p)
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
    d = _project_dict(p)
    d["member_count"], d["source_count"] = len(members), len(srcs)
    d["members"] = [_member_dict(m) for m in members]
    d["source_ids"] = list(srcs)
    return d

async def get_capabilities(pid: str) -> dict:
    async with db.session() as s:
        p = (await s.execute(select(Project).where(Project.id == pid))).scalar_one_or_none()
    if p is None:
        return dict(TEAM_CAPS)
    return {k: bool(getattr(p, k)) for k in _CAP_KEYS}

async def source_ids_for_project_terms(terms: list) -> set:
    if not terms:
        return set()
    async with db.session() as s:
        ps = (await s.execute(select(Project).where(Project.kind == "team"))).scalars().all()
    proj_ids = [p.id for p in ps if any(t in p.name.lower() for t in terms)]
    if not proj_ids:
        return set()
    async with db.session() as s:
        rows = (await s.execute(
            select(ProjectSource.source_id)
            .where(ProjectSource.project_id.in_(proj_ids)))).scalars().all()
    return set(rows)

async def create_project(name: str, description: str = "", owner: str = "",
                         kind: str = "team", **caps) -> dict:
    owner = owner or DEFAULT_USER
    settings = dict(PERSONAL_CAPS if kind == "personal" else TEAM_CAPS)
    settings.update({k: bool(v) for k, v in caps.items() if k in _CAP_KEYS})
    pid = make_id()
    p = Project(id=pid, name=name, description=description, completed=False,
                created_at=_ts(), kind=kind,
                owner_user=owner if kind == "personal" else "", **settings)
    async with db.session() as s:
        s.add(p)
        s.add(ProjectMember(project_id=pid, name=owner, role=OWNER_ROLE,
                            color=PALETTE[0]))
        await s.commit()
    return await get_project(pid)

async def personal_project(user: str) -> dict:
    user = user or DEFAULT_USER
    async with db.session() as s:
        p = (await s.execute(
            select(Project).where(Project.kind == "personal",
                                  Project.owner_user == user))).scalar_one_or_none()
    if p is not None:
        return await get_project(p.id)
    return await create_project("My workspace", "Personal workspace",
                                owner=user, kind="personal")

async def personal_project_id(user: str) -> str:
    return (await personal_project(user))["id"]

async def update_project(pid: str, name=None, description=None,
                         completed=None, **caps) -> dict | None:
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
        for k in _CAP_KEYS:
            if caps.get(k) is not None:
                setattr(p, k, bool(caps[k]))
        await s.commit()
    return await get_project(pid)

async def delete_project(pid: str) -> None:
    async with db.session() as s:
        p = (await s.execute(select(Project).where(Project.id == pid))).scalar_one_or_none()
        if p and (p.kind or "team") == "personal":
            raise ValueError("a personal workspace cannot be deleted")
        if p:
            await s.delete(p)
        await s.execute(delete(ProjectMember).where(ProjectMember.project_id == pid))
        await s.execute(delete(ProjectSource).where(ProjectSource.project_id == pid))
        await s.execute(delete(ProjectFolder).where(ProjectFolder.project_id == pid))
        await s.execute(delete(SourceQuiz).where(SourceQuiz.project_id == pid))
        await s.commit()
    from . import notes  # local import: notes.py depends on this module
    await notes.delete_for_project(pid)

async def list_members(pid: str) -> list:
    async with db.session() as s:
        ms = (await s.execute(
            select(ProjectMember).where(ProjectMember.project_id == pid)
            .order_by(ProjectMember.id))).scalars().all()
    return [_member_dict(m) for m in ms]

async def member_role(pid: str, user: str) -> str | None:
    async with db.session() as s:
        m = (await s.execute(
            select(ProjectMember).where(ProjectMember.project_id == pid,
                                        ProjectMember.name == user)
        )).scalar_one_or_none()
    return m.role if m else None

async def color_map(pid: str) -> dict:
    return {m["name"]: m["color"] for m in await list_members(pid)}

def _pick_color(taken: set) -> str:
    for c in PALETTE:
        if c not in taken:
            return c
    return PALETTE[len(taken) % len(PALETTE)]

async def add_member(pid: str, name: str, role: str = DEFAULT_ROLE) -> dict | None:
    role = role if role in ROLES else DEFAULT_ROLE
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
        taken = set((await s.execute(
            select(ProjectMember.color)
            .where(ProjectMember.project_id == pid))).scalars().all())
        color = _pick_color(taken)
        s.add(ProjectMember(project_id=pid, name=name, role=role, color=color))
        await s.commit()
    return {"name": name, "role": role, "color": color}

async def remove_member(pid: str, name: str) -> bool:
    async with db.session() as s:
        m = (await s.execute(
            select(ProjectMember).where(ProjectMember.project_id == pid,
                                        ProjectMember.name == name)
        )).scalar_one_or_none()
        if m is None:
            return False
        if m.role == OWNER_ROLE:
            owners = (await s.execute(
                select(func.count()).select_from(ProjectMember)
                .where(ProjectMember.project_id == pid,
                       ProjectMember.role == OWNER_ROLE))).scalar_one()
            if owners <= 1:
                raise ValueError(f"cannot remove the last {OWNER_ROLE} of a project")
        await s.delete(m)
        await s.commit()
    return True

async def set_member_role(pid: str, name: str, role: str) -> bool:
    role = role if role in ROLES else DEFAULT_ROLE
    async with db.session() as s:
        m = (await s.execute(
            select(ProjectMember).where(ProjectMember.project_id == pid,
                                        ProjectMember.name == name)
        )).scalar_one_or_none()
        if m is None:
            return False
        if m.role == OWNER_ROLE and role != OWNER_ROLE:
            owners = (await s.execute(
                select(func.count()).select_from(ProjectMember)
                .where(ProjectMember.project_id == pid,
                       ProjectMember.role == OWNER_ROLE))).scalar_one()
            if owners <= 1:
                raise ValueError(f"cannot demote the last {OWNER_ROLE} of a project")
        m.role = role
        await s.commit()
    return True

async def add_source(pid: str, source_id: str, folder: str = "",
                     added_by: str = "") -> bool:
    async with db.session() as s:
        exists = (await s.execute(
            select(ProjectSource).where(ProjectSource.project_id == pid,
                                        ProjectSource.source_id == source_id)
        )).scalar_one_or_none()
        if exists:
            return False
        s.add(ProjectSource(project_id=pid, source_id=source_id, added_at=_ts(),
                            folder=norm_folder(folder),
                            added_by=added_by or DEFAULT_USER))
        await s.commit()
    return True

async def has_source(pid: str, source_id: str) -> bool:
    async with db.session() as s:
        return (await s.execute(
            select(ProjectSource).where(ProjectSource.project_id == pid,
                                        ProjectSource.source_id == source_id)
        )).scalar_one_or_none() is not None

async def remove_source(pid: str, source_id: str) -> bool:
    async with db.session() as s:
        link = (await s.execute(
            select(ProjectSource).where(ProjectSource.project_id == pid,
                                        ProjectSource.source_id == source_id)
        )).scalar_one_or_none()
        if link is None:
            return False
        await s.delete(link)
        await s.execute(delete(SourceQuiz).where(SourceQuiz.project_id == pid,
                                                 SourceQuiz.source_id == source_id))
        await s.commit()
    from . import anchors, notes
    await notes.delete_for_source(pid, source_id)
    await anchors.delete_for_project_source(pid, source_id)
    return True

async def remove_source_everywhere(source_id: str) -> None:
    """Personal source deletion must also drop its project links."""
    async with db.session() as s:
        await s.execute(delete(ProjectSource).where(ProjectSource.source_id == source_id))
        await s.execute(delete(SourceQuiz).where(SourceQuiz.source_id == source_id))
        await s.commit()
    from . import anchors, notes
    await notes.delete_for_source(None, source_id)
    await anchors.delete_for_project_source(None, source_id)

async def set_source_folder(pid: str, source_id: str, folder: str) -> bool:
    folder = norm_folder(folder)
    async with db.session() as s:
        link = (await s.execute(
            select(ProjectSource).where(ProjectSource.project_id == pid,
                                        ProjectSource.source_id == source_id)
        )).scalar_one_or_none()
        if link is None:
            return False
        link.folder = folder
        await s.commit()
    if folder:
        await create_folder(pid, folder)
    return True

async def source_links(pid: str) -> list:
    async with db.session() as s:
        rows = (await s.execute(
            select(ProjectSource).where(ProjectSource.project_id == pid)
            .order_by(ProjectSource.added_at))).scalars().all()
    return [{"source_id": r.source_id, "folder": r.folder or "",
             "added_at": r.added_at, "added_by": r.added_by or ""} for r in rows]

async def projects_holding_source(source_id: str) -> list:
    async with db.session() as s:
        return list((await s.execute(
            select(ProjectSource.project_id)
            .where(ProjectSource.source_id == source_id))).scalars().all())

async def list_folders(pid: str) -> list:
    async with db.session() as s:
        rows = set((await s.execute(
            select(ProjectFolder.path)
            .where(ProjectFolder.project_id == pid))).scalars().all())
        rows |= set((await s.execute(
            select(ProjectSource.folder)
            .where(ProjectSource.project_id == pid))).scalars().all())
    out = set()
    for path in rows:
        path = norm_folder(path)
        parts = path.split("/") if path else []
        for i in range(1, len(parts) + 1):   # every ancestor exists too
            out.add("/".join(parts[:i]))
    out.discard("")
    return sorted(out)

async def create_folder(pid: str, path: str) -> str:
    path = norm_folder(path)
    if not path:
        raise ValueError("folder path required")
    parts = path.split("/")
    async with db.session() as s:
        known = set((await s.execute(
            select(ProjectFolder.path)
            .where(ProjectFolder.project_id == pid))).scalars().all())
        for i in range(1, len(parts) + 1):
            anc = "/".join(parts[:i])
            if anc not in known:
                s.add(ProjectFolder(project_id=pid, path=anc, created_at=_ts()))
                known.add(anc)
        await s.commit()
    return path

async def rename_folder(pid: str, path: str, new_path: str) -> str:
    path, new_path = norm_folder(path), norm_folder(new_path)
    if not path or not new_path:
        raise ValueError("folder path required")
    if new_path == path:
        return new_path
    if (new_path + "/").startswith(path + "/"):
        raise ValueError("cannot move a folder inside itself")

    def moved(p: str) -> str | None:
        if p == path:
            return new_path
        if p.startswith(path + "/"):
            return new_path + p[len(path):]
        return None

    async with db.session() as s:
        for row in (await s.execute(select(ProjectFolder)
                                    .where(ProjectFolder.project_id == pid))).scalars():
            dest = moved(row.path or "")
            if dest is not None:
                row.path = dest
        for row in (await s.execute(select(ProjectSource)
                                    .where(ProjectSource.project_id == pid))).scalars():
            dest = moved(row.folder or "")
            if dest is not None:
                row.folder = dest
        await s.commit()
    from . import notes
    await notes.move_folder(pid, path, new_path)
    await create_folder(pid, new_path)
    return new_path

async def delete_folder(pid: str, path: str) -> None:
    path = norm_folder(path)
    if not path:
        raise ValueError("folder path required")
    parent = path.rsplit("/", 1)[0] if "/" in path else ""

    def lifted(p: str) -> str | None:
        if p == path:
            return parent
        if p.startswith(path + "/"):
            tail = p[len(path) + 1:]
            return f"{parent}/{tail}" if parent else tail
        return None

    async with db.session() as s:
        for row in (await s.execute(select(ProjectSource)
                                    .where(ProjectSource.project_id == pid))).scalars():
            dest = lifted(row.folder or "")
            if dest is not None:
                row.folder = dest
        for row in (await s.execute(select(ProjectFolder)
                                    .where(ProjectFolder.project_id == pid))).scalars():
            if (row.path or "") == path or (row.path or "").startswith(path + "/"):
                await s.delete(row)
        await s.commit()
    from . import notes
    await notes.lift_folder(pid, path, parent)
