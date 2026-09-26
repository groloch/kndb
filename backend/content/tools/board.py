"""The agent's tools over the kanban board: reading it as structure, adding
cards, moving them between columns, and creating columns.

Scoped by the review: the agent may create cards, columns, and move cards
between columns, but never renames or deletes — card fields are still in
flux (checkable steps are planned), so there is no update_card tool yet.
Cards are created with the calling user as author (``wants_user``).
"""

import json

from ...data import board, projects


async def _board_view(pid: str) -> dict:
    """The board trimmed to what the agent reasons about: columns with their
    counts, active cards without timestamps, archived cards as a count
    """
    b = await board.get_board(pid)
    columns = [{"id": c["id"], "name": c["name"], "position": c["position"],
                "wip_limit": c.get("wip_limit"), "card_count": c["card_count"]}
               for c in b["columns"]]
    cards = [{"id": c["id"], "title": c["title"], "column_id": c["column_id"],
              "assignees": c["assignees"], "labels": c["labels"],
              "due_date": c["due_date"], "source_id": c["source_id"]}
             for c in b["cards"]]
    return {"columns": columns, "cards": cards, "archived": len(b["archived"])}


async def _get_board(pid: str):
    return json.dumps(await _board_view(pid))


async def _resolve_column(pid: str, column: str):
    """A column named by the model — name (case-insensitive) or id — to its
    column dict, None when nothing matches
    """
    view = await _board_view(pid)
    want = (column or "").strip().lower()
    for c in view["columns"]:
        if want and (c["id"] == want or c["name"].strip().lower() == want):
            return c
    return None


async def _create_card(pid: str, user: str, column: str, title: str,
                       description: str = "", assignees: list = None,
                       labels: list = None, due_date: str = ""):
    col = await _resolve_column(pid, column)
    if col is None:
        return (f"Unknown column {column!r}. Call get_board first and use a "
                f"column's name or id.")
    assignees = [a for a in (assignees or []) if isinstance(a, str) and a.strip()]
    if assignees:
        members = {m["name"] for m in await projects.list_members(pid)}
        unknown = [a for a in assignees if a not in members]
        if unknown:
            names = ", ".join(sorted(members))
            return (f"Unknown assignee(s): {', '.join(unknown)}. "
                    f"Members are: {names}.")
    card = await board.create_card(
        pid, col["id"], title=title,
        description=description or "", assignees=assignees,
        labels=[l for l in (labels or []) if isinstance(l, str)],
        due_date=due_date or "", author=user)
    return json.dumps({"created": card["id"], "title": card["title"],
                       "column": col["name"]})


async def _move_card(pid: str, card_id: str, column: str = "",
                     position: int = None):
    column_id = None
    if (column or "").strip():
        col = await _resolve_column(pid, column)
        if col is None:
            return f"Unknown column {column!r}. Call get_board for the columns."
        column_id = col["id"]
    card = await board.move_card(pid, card_id, column_id=column_id,
                                 position=position)
    if card is None:
        return f"Card {card_id} not found in this project."
    return json.dumps({"moved": card["id"], "title": card["title"],
                       "column_id": card["column_id"],
                       "position": card["position"]})


def _unquote(name: str) -> str:
    """A column name with the wrapping whitespace or a single layer of stray
    quotes stripped — the model occasionally wraps string arguments in them
    """
    name = (name or "").strip()
    if len(name) >= 2 and name[0] == name[-1] and name[0] in "'\"":
        name = name[1:-1].strip()
    return name


async def _create_column(pid: str, name: str, position: int = None):
    name = _unquote(name)
    if not name:
        return "A column needs a name."
    try:
        col = await board.create_column(pid, name, position=position)
    except ValueError as e:
        return f"could not create column: {e}"
    return json.dumps({"created": col["id"], "name": col["name"],
                       "position": col["position"]})


_get_board_tool = {
    "security": "read",
    "function": _get_board,
    "tool_dict": {
        "name": "get_board",
        "description": "Read the project's kanban board: its columns and the active cards in them (title, assignees, labels, due date), plus how many cards are archived.",
        "parameters": {
            "type": "object",
            "properties": {

            }
        }
    }
}
_create_card_tool = {
    "security": "write",
    "wants_user": True,
    "function": _create_card,
    "tool_dict": {
        "name": "create_card",
        "description": "Create a card at the bottom of a board column, as the calling user. The column is named by its title or ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "column": {"type": "string", "description": "Column name or ID."},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "assignees": {"type": "array", "items": {"type": "string"},
                              "description": "Member names."},
                "labels": {"type": "array", "items": {"type": "string"}},
                "due_date": {"type": "string", "description": "YYYY-MM-DD, optional."}
            },
            "required": ["column", "title"]
        }
    }
}
_move_card_tool = {
    "security": "write",
    "function": _move_card,
    "tool_dict": {
        "name": "move_card",
        "description": "Move a board card to another column (by name or ID) and/or to a position in it. Never renames or deletes; position 0 is the top.",
        "parameters": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string"},
                "column": {"type": "string", "description": "Target column name or ID; empty to keep the column."},
                "position": {"type": "integer", "description": "Position in the target column; omitted to drop at the end."}
            },
            "required": ["card_id"]
        }
    }
}
_create_column_tool = {
    "security": "write",
    "function": _create_column,
    "tool_dict": {
        "name": "create_column",
        "description": "Create a board column, by default at the end (position 0 puts it first).",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "position": {"type": "integer", "description": "Where to place it among the columns; omitted for the end."}
            },
            "required": ["name"]
        }
    }
}


TOOLS = (
    _get_board_tool,
    _create_column_tool,
    _create_card_tool,
    _move_card_tool,
)