"""A project's kanban board: the columns and cards, and their order.
Keyed by project id alone — there is one board per project, no ``boards`` row
of its own. Cards never own text lines, so nothing here is blame-tracked:
a writer may create, edit, move and delete any card, and ordering is
last-write-wins with positions reindexed on every mutation
"""

import json
import re
import secrets
import time

from sqlalchemy import delete, func, select, update

from ..core import db
from ..core.db import BoardCard, BoardColumn


_DUE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def make_id(col: bool) -> str:
    """A fresh column or card id, prefixed ``col_`` or ``crd_``
    """
    return ("col_" if col else "crd_") + secrets.token_hex(6)

def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")

def _loads(s, default: list) -> list:
    """Parsed JSON, the default when the column is empty or corrupt
    """
    if not s:
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default

def _dumps(v) -> str:
    return json.dumps(v, ensure_ascii=False)

def _norm_list(items) -> list:
    """A list of strings, blanked and deduped, order kept
    """
    out = []
    for x in items or []:
        v = str(x).strip()
        if v and v not in out:
            out.append(v)
    return out[:50]

def _column_dict(c: BoardColumn) -> dict:
    return {
        "id": c.id,
        "name": c.name or "",
        "position": c.position or 0,
        "wip_limit": c.wip_limit or 0,
        "color": c.color or "",
        "created_at": c.created_at or "",
        "card_count": 0,  # filled by get_board
    }

def _card_dict(c: BoardCard) -> dict:
    return {
        "id": c.id,
        "column_id": c.column_id,
        "title": c.title or "",
        "description": c.description or "",
        "labels": _loads(c.labels, []),
        "assignees": _loads(c.assignees, []),
        "due_date": c.due_date or "",
        "position": c.position or 0,
        "archived": bool(c.archived),
        "source_id": c.source_id or "",
        "created_by": c.created_by or "",
        "created_at": c.created_at or "",
        "updated_at": c.updated_at or "",
    }

async def _reindex_columns(s, pid: str) -> None:
    """Rewrites dense 0..n-1 positions on a project's columns, in place
    """
    rows = (await s.execute(
        select(BoardColumn).where(BoardColumn.project_id == pid)
        .order_by(BoardColumn.position, BoardColumn.created_at))).scalars().all()
    for i, r in enumerate(rows):
        r.position = i

async def _reindex_cards(s, column_id: str) -> None:
    """Rewrites dense 0..n-1 positions on a column's cards, in place
    """
    rows = (await s.execute(
        select(BoardCard).where(BoardCard.column_id == column_id)
        .order_by(BoardCard.position, BoardCard.created_at))).scalars().all()
    for i, r in enumerate(rows):
        r.position = i


async def get_board(pid: str) -> dict:
    """Everything the board shows: columns, active cards and archived ones.
    Columns and cards ordered, card_count held per column and counting active
    cards only, since archived ones never count toward a WIP limit
    """
    async with db.session() as s:
        cols = (await s.execute(
            select(BoardColumn).where(BoardColumn.project_id == pid)
            .order_by(BoardColumn.position, BoardColumn.created_at))).scalars().all()
        cards = (await s.execute(
            select(BoardCard).where(BoardCard.project_id == pid)
            .order_by(BoardCard.position, BoardCard.created_at))).scalars().all()
    columns = [_column_dict(c) for c in cols]
    active, archived = [], []
    for c in cards:
        d = _card_dict(c)
        (archived if d["archived"] else active).append(d)
    counts = {}
    for d in active:
        counts[d["column_id"]] = counts.get(d["column_id"], 0) + 1
    for col in columns:
        col["card_count"] = counts.get(col["id"], 0)
    return {"columns": columns, "cards": active, "archived": archived}


async def create_column(pid: str, name: str, *, position: int | None = None) -> dict:
    """The new column, appended to the end unless position inserts it elsewhere.
    Raises ValueError on a blank name
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("column name required")
    cid = make_id(True)
    async with db.session() as s:
        order = list((await s.execute(
            select(BoardColumn.id).where(BoardColumn.project_id == pid)
            .order_by(BoardColumn.position))).scalars().all())
        if position is None:
            order.append(cid)
        else:
            order.insert(max(0, min(int(position), len(order))), cid)
        rows = (await s.execute(
            select(BoardColumn).where(BoardColumn.project_id == pid))).scalars().all()
        row = BoardColumn(id=cid, project_id=pid, name=name, position=0,
                          created_at=_ts())
        s.add(row)
        by_id = {r.id: r for r in rows}
        by_id[cid] = row
        for i, c2 in enumerate(order):
            by_id[c2].position = i
        await s.commit()
    return _column_dict(row)

async def update_column(pid: str, cid: str, *, name=None, wip_limit=None,
                        color=None, position=None) -> dict | None:
    """The column after a rename, WIP limit, colour or move, None when unknown.
    Only the keys given change, and moving reorders the list around it.
    Raises ValueError on a blank name or a negative WIP limit
    """
    async with db.session() as s:
        row = (await s.execute(
            select(BoardColumn).where(BoardColumn.id == cid))).scalar_one_or_none()
        if row is None or row.project_id != pid:
            return None
        if name is not None:
            name = (name or "").strip()
            if not name:
                raise ValueError("column name required")
            row.name = name
        if wip_limit is not None:
            wip = int(wip_limit)
            if wip < 0:
                raise ValueError("wip limit cannot be negative")
            row.wip_limit = wip
        if color is not None:
            row.color = (color or "").strip()
        if position is not None:
            order = list((await s.execute(
                select(BoardColumn.id).where(BoardColumn.project_id == pid)
                .order_by(BoardColumn.position))).scalars().all())
            order.remove(cid)
            order.insert(max(0, min(int(position), len(order))), cid)
            rows = (await s.execute(
                select(BoardColumn).where(BoardColumn.project_id == pid))).scalars().all()
            by_id = {r.id: r for r in rows}
            for i, c2 in enumerate(order):
                by_id[c2].position = i
        await s.commit()
    return _column_dict(row)

async def reorder_columns(pid: str, column_ids: list) -> None:
    """Rewrites the column order from the given id list, columns it misses
    keeping their relative order after those it names.
    Raises ValueError on an id that is not a column of this project
    """
    ids = [x for x in (column_ids or []) if isinstance(x, str) and x]
    async with db.session() as s:
        rows = (await s.execute(
            select(BoardColumn).where(BoardColumn.project_id == pid)
            .order_by(BoardColumn.position))).scalars().all()
        by_id = {r.id: r for r in rows}
        order, seen = [], set()
        for cid in ids:
            if cid not in by_id:
                raise ValueError(f"unknown column {cid}")
            if cid not in seen:
                order.append(cid)
                seen.add(cid)
        order += [r.id for r in rows if r.id not in seen]
        for i, c2 in enumerate(order):
            by_id[c2].position = i
        await s.commit()

async def delete_column(pid: str, cid: str) -> bool:
    """Deletes a column and the cards in it, False when it is unknown.
    Raises ValueError on the project's last column, which cannot go
    """
    async with db.session() as s:
        row = (await s.execute(
            select(BoardColumn).where(BoardColumn.id == cid))).scalar_one_or_none()
        if row is None or row.project_id != pid:
            return False
        ncols = (await s.execute(
            select(func.count()).select_from(BoardColumn)
            .where(BoardColumn.project_id == pid))).scalar_one()
        if ncols <= 1:
            raise ValueError("cannot delete the last column")
        await s.delete(row)
        await s.execute(delete(BoardCard).where(BoardCard.column_id == cid))
        await _reindex_columns(s, pid)
        await s.commit()
    return True


async def create_card(pid: str, column_id: str, *, title: str,
                      description: str = "", labels=(), assignees=(),
                      due_date: str = "", source_id: str = "",
                      author: str = "") -> dict:
    """The new card at the bottom of a column, raises ValueError on a blank
    title, an unknown column or a malformed due date.
    ``source_id`` is trusted to name a source of this project — the route
    verifies it
    """
    title = (title or "").strip()
    if not title:
        raise ValueError("card title required")
    due = (due_date or "").strip()
    if due and not _DUE_RE.match(due):
        raise ValueError("due_date must be YYYY-MM-DD")
    cid = make_id(False)
    async with db.session() as s:
        col = (await s.execute(
            select(BoardColumn).where(BoardColumn.id == column_id))).scalar_one_or_none()
        if col is None or col.project_id != pid:
            raise ValueError(f"unknown column {column_id}")
        last = (await s.execute(
            select(func.max(BoardCard.position))
            .where(BoardCard.column_id == column_id))).scalar()
        card = BoardCard(
            id=cid, project_id=pid, column_id=column_id, title=title,
            description=(description or "").strip(),
            labels=_dumps(_norm_list(labels)),
            assignees=_dumps(_norm_list(assignees)),
            due_date=due,
            position=(last + 1) if last is not None else 0,
            source_id=(source_id or "").strip(),
            created_by=(author or "").strip(),
            created_at=_ts(), updated_at=_ts())
        s.add(card)
        await s.commit()
    return _card_dict(card)

async def update_card(pid: str, cid: str, *, title=None, description=None,
                      labels=None, assignees=None, due_date=None,
                      source_id=None, archived=None) -> dict | None:
    """The card after a partial change, None when it is unknown.
    Raises ValueError on a blank title or a malformed due date
    """
    async with db.session() as s:
        row = (await s.execute(
            select(BoardCard).where(BoardCard.id == cid))).scalar_one_or_none()
        if row is None or row.project_id != pid:
            return None
        if title is not None:
            title = (title or "").strip()
            if not title:
                raise ValueError("card title required")
            row.title = title
        if description is not None:
            row.description = (description or "").strip()
        if labels is not None:
            row.labels = _dumps(_norm_list(labels))
        if assignees is not None:
            row.assignees = _dumps(_norm_list(assignees))
        if due_date is not None:
            due = (due_date or "").strip()
            if due and not _DUE_RE.match(due):
                raise ValueError("due_date must be YYYY-MM-DD")
            row.due_date = due
        if source_id is not None:
            row.source_id = (source_id or "").strip()
        if archived is not None:
            row.archived = bool(archived)
        row.updated_at = _ts()
        await s.commit()
    return _card_dict(row)

async def delete_card(pid: str, cid: str) -> bool:
    """Deletes a card, False when it is unknown, reindexing what is left
    """
    async with db.session() as s:
        row = (await s.execute(
            select(BoardCard).where(BoardCard.id == cid))).scalar_one_or_none()
        if row is None or row.project_id != pid:
            return False
        column_id = row.column_id
        await s.delete(row)
        await _reindex_cards(s, column_id)
        await s.commit()
    return True

async def move_card(pid: str, cid: str, *, column_id: str | None = None,
                    position: int | None = None) -> dict | None:
    """The card after a move to another column or position, None when it is
    unknown.
    Both the column it left (if any) and the one it lands in are reindexed.
    Raises ValueError on a target column that is not this project's
    """
    position = 0 if position is None else max(0, int(position))
    async with db.session() as s:
        card = (await s.execute(
            select(BoardCard).where(BoardCard.id == cid))).scalar_one_or_none()
        if card is None or card.project_id != pid:
            return None
        target = card.column_id if column_id is None else (column_id or "").strip()
        tcol = (await s.execute(
            select(BoardColumn).where(BoardColumn.id == target))).scalar_one_or_none()
        if tcol is None or tcol.project_id != pid:
            raise ValueError(f"unknown column {target}")
        source = card.column_id
        order = [r.id for r in (await s.execute(
            select(BoardCard).where(BoardCard.column_id == target)
            .order_by(BoardCard.position))).scalars().all() if r.id != cid]
        order.insert(min(position, len(order)), cid)
        card.column_id = target
        card.updated_at = _ts()
        rows = (await s.execute(
            select(BoardCard).where(BoardCard.column_id == target))).scalars().all()
        by_id = {r.id: r for r in rows}
        for i, c2 in enumerate(order):
            by_id[c2].position = i
        if target != source:
            await _reindex_cards(s, source)
        await s.commit()
    return _card_dict(card)


async def sweep_source(pid: str | None, source_id: str) -> None:
    """Severs the cards pointing at a source, in one project or every one.
    The card text survives; only the link goes
    """
    stmt = update(BoardCard).where(BoardCard.source_id == (source_id or ""))
    if pid is not None:
        stmt = stmt.where(BoardCard.project_id == pid)
    async with db.session() as s:
        await s.execute(stmt.values(source_id=""))
        await s.commit()

async def sweep_member(pid: str, name: str) -> None:
    """Drops a member's name from the assignees of every card, so their
    removal leaves no ghost assignment behind
    """
    name = (name or "").strip()
    if not name:
        return
    async with db.session() as s:
        rows = (await s.execute(
            select(BoardCard).where(BoardCard.project_id == pid))).scalars().all()
        for row in rows:
            who = _loads(row.assignees, [])
            kept = [a for a in who if a != name]
            if len(kept) != len(who):
                row.assignees = _dumps(kept)
        await s.commit()