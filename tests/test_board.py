"""End-to-end tests for the per-project kanban board.

These cover the rules that are easy to break and expensive to get wrong: who
may write, what survives a project/source/member deletion, and that one
project's board never leaks into another's
"""

import io

import pytest


def mkuser(client, name):
    client.post("/api/users", json={"name": name})
    return {"X-KNDB-User": name}


def import_md(client, headers, title, body="hello", project=""):
    data = {"project": project}
    files = {"file": (f"{title}.md", io.BytesIO(body.encode()), "text/markdown")}
    r = client.post("/api/import", data=data, files=files, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def mkproject(client, headers, name):
    r = client.post("/api/projects", json={"name": name}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["project"]


def board(client, headers, pid):
    """The whole board payload, once the project exists
    """
    r = client.get(f"/api/projects/{pid}/board", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["board"]


def addcol(client, headers, pid, name):
    r = client.post(f"/api/projects/{pid}/board/columns",
                    json={"name": name}, headers=headers)
    assert r.status_code == 200, r.text
    cols = r.json()["board"]["columns"]
    return next(x for x in cols if x["name"] == name)


def addcard(client, headers, pid, col, title, **kw):
    r = client.post(f"/api/projects/{pid}/board/cards",
                    json={"column_id": col, "title": title, **kw}, headers=headers)
    assert r.status_code == 200, r.text
    cards = r.json()["board"]["cards"]
    return next(x for x in cards if x["title"] == title)


# --- lifecycle --------------------------------------------------------------

def test_board_lifecycle(client):
    """A column and a card can be made, edited and taken down, the board
    staying coherent at every step"""
    h = mkuser(client, "owner")
    pid = mkproject(client, h, "Board life")["id"]
    todo = addcol(client, h, pid, "To do")
    done = addcol(client, h, pid, "Done")

    c = addcard(client, h, pid, todo["id"], "Write the plan",
                labels=["research", "mvp"], assignees=["owner"],
                due_date="2025-12-31")
    assert c["labels"] == ["research", "mvp"]
    assert c["assignees"] == ["owner"]
    assert c["due_date"] == "2025-12-31"

    b = board(client, h, pid)
    assert [x["name"] for x in b["columns"]] == ["To do", "Done"]
    assert b["cards"][0]["title"] == "Write the plan"
    assert b["columns"][0]["card_count"] == 1
    assert b["archived"] == []

    # a blank title is not a card
    r = client.post(f"/api/projects/{pid}/board/cards",
                    json={"column_id": todo["id"], "title": "  "}, headers=h)
    assert r.status_code == 400

    # patch is partial: the labels survive the rename
    r = client.patch(f"/api/projects/{pid}/board/cards/{c['id']}",
                     json={"title": "Rewrite the plan"}, headers=h)
    c2 = next(x for x in r.json()["board"]["cards"] if x["id"] == c["id"])
    assert c2["title"] == "Rewrite the plan"
    assert c2["labels"] == ["research", "mvp"]

    # patch the column's WIP limit
    r = client.patch(f"/api/projects/{pid}/board/columns/{todo['id']}",
                     json={"wip_limit": 3}, headers=h)
    assert next(x for x in r.json()["board"]["columns"]
                if x["id"] == todo["id"])["wip_limit"] == 3

    # archiving parks the card, restoring brings it back
    r = client.patch(f"/api/projects/{pid}/board/cards/{c['id']}",
                     json={"archived": True}, headers=h)
    assert r.json()["board"]["cards"] == []
    assert [x["id"] for x in r.json()["board"]["archived"]] == [c["id"]]
    client.patch(f"/api/projects/{pid}/board/cards/{c['id']}",
                 json={"archived": False}, headers=h)

    # deleting the card
    assert client.delete(f"/api/projects/{pid}/board/cards/{c['id']}",
                         headers=h).status_code == 200

    # deleting the column takes its cards; the last column cannot go
    assert client.delete(f"/api/projects/{pid}/board/columns/{todo['id']}",
                         headers=h).status_code == 200
    assert [x["name"] for x in board(client, h, pid)["columns"]] == ["Done"]
    r = client.delete(f"/api/projects/{pid}/board/columns/{done['id']}",
                      headers=h)
    assert r.status_code == 409


# --- ordering ---------------------------------------------------------------

def test_board_ordering(client):
    """Moves reindex both columns, and a full list replaces the column order"""
    h = mkuser(client, "orderer")
    pid = mkproject(client, h, "Ordering")["id"]
    a = addcol(client, h, pid, "A")["id"]
    b = addcol(client, h, pid, "B")["id"]
    c = addcol(client, h, pid, "C")["id"]
    c1 = addcard(client, h, pid, a, "one")["id"]
    c2 = addcard(client, h, pid, a, "two")["id"]
    c3 = addcard(client, h, pid, b, "three")["id"]

    # same-column move to the end
    r = client.post(f"/api/projects/{pid}/board/move",
                    json={"card_id": c1, "column_id": a, "position": 1},
                    headers=h)
    cards = {x["id"]: x for x in r.json()["board"]["cards"]}
    assert (cards[c2]["position"], cards[c1]["position"]) == (0, 1)

    # cross-column move: both columns reindexed, dense 0..n-1
    r = client.post(f"/api/projects/{pid}/board/move",
                    json={"card_id": c1, "column_id": b, "position": 0},
                    headers=h)
    cards = {x["id"]: x for x in r.json()["board"]["cards"]}
    assert cards[c1]["column_id"] == b and cards[c1]["position"] == 0
    assert cards[c3]["position"] == 1
    assert cards[c2]["position"] == 0

    # column order by full list
    r = client.post(f"/api/projects/{pid}/board/columns/order",
                    json={"column_ids": [c, a, b]}, headers=h)
    assert [x["id"] for x in r.json()["board"]["columns"]] == [c, a, b]

    # a list missing columns keeps the rest appended in their order
    r = client.post(f"/api/projects/{pid}/board/columns/order",
                    json={"column_ids": [b]}, headers=h)
    assert [x["id"] for x in r.json()["board"]["columns"]] == [b, c, a]


# --- permissions ------------------------------------------------------------

def test_board_permissions(client):
    """Reading needs membership, writing a writing role, and a completed
    project refuses both kinds of write"""
    h = mkuser(client, "boss")
    pid = mkproject(client, h, "Roles")["id"]
    outsider = mkuser(client, "outsider")

    assert client.get(f"/api/projects/{pid}/board", headers=outsider).status_code == 403
    assert client.get(f"/api/projects/{pid}/board", headers=h).status_code == 200

    client.post(f"/api/projects/{pid}/members",
                json={"name": "peeper", "role": "spectator"}, headers=h)
    peeper = {"X-KNDB-User": "peeper"}
    assert client.get(f"/api/projects/{pid}/board", headers=peeper).status_code == 200
    assert client.post(f"/api/projects/{pid}/board/columns",
                       json={"name": "Nope"}, headers=peeper).status_code == 403

    client.post(f"/api/projects/{pid}/members",
                json={"name": "writer", "role": "contributor"}, headers=h)
    wr = {"X-KNDB-User": "writer"}
    assert client.post(f"/api/projects/{pid}/board/columns",
                       json={"name": "Backlog"}, headers=wr).status_code == 200

    # a completed project is read-only
    client.post(f"/api/projects/{pid}", json={"completed": True}, headers=h)
    assert client.post(f"/api/projects/{pid}/board/columns",
                       json={"name": "Nope"}, headers=wr).status_code == 403
    client.post(f"/api/projects/{pid}", json={"completed": False}, headers=h)
    assert client.post(f"/api/projects/{pid}/board/columns",
                       json={"name": "Yep"}, headers=wr).status_code == 200


# --- isolation --------------------------------------------------------------

def test_board_isolation(client):
    """Two projects never see each other's board, and ids do not cross"""
    h = mkuser(client, "iso")
    p1 = mkproject(client, h, "One")["id"]
    p2 = mkproject(client, h, "Two")["id"]
    col = addcol(client, h, p1, "P1 col")["id"]
    card = addcard(client, h, p1, col, "P1 card")

    assert board(client, h, p2)["columns"] == []

    assert client.patch(f"/api/projects/{p2}/board/cards/{card['id']}",
                        json={"title": "hijack"}, headers=h).status_code == 404
    assert client.post(f"/api/projects/{p2}/board/move",
                       json={"card_id": card["id"], "column_id": col},
                       headers=h).status_code == 404
    assert client.delete(f"/api/projects/{p2}/board/columns/{col}",
                         headers=h).status_code == 404
    assert client.post(f"/api/projects/{p2}/board/cards",
                       json={"column_id": col, "title": "x"},
                       headers=h).status_code == 400


# --- sweeps -----------------------------------------------------------------

def test_board_sweeps(client):
    """A source's death severs the card links, a member's removal their
    assignees, and a project's deletion its whole board"""
    h = mkuser(client, "sweeper")
    pid = mkproject(client, h, "Sweeps")["id"]
    sid = import_md(client, h, "Source A", project=pid)
    col = addcol(client, h, pid, "Doing")["id"]
    card = addcard(client, h, pid, col, "Read it", source_id=sid)
    assert card["source_id"] == sid

    # removing the source from the project severs this project's links only
    client.delete(f"/api/projects/{pid}/sources/{sid}", headers=h)
    assert next(x for x in board(client, h, pid)["cards"]
                if x["id"] == card["id"])["source_id"] == ""

    # deleting the source everywhere does the same in every project
    client.post(f"/api/projects/{pid}/sources", json={"source_id": sid},
                headers=h)
    c2 = addcard(client, h, pid, col, "linked again", source_id=sid)
    assert client.delete(f"/api/source/{sid}", headers=h).status_code == 200
    assert next(x for x in board(client, h, pid)["cards"]
                if x["id"] == c2["id"])["source_id"] == ""

    # removing a member drops their name from assignees
    client.post(f"/api/projects/{pid}/members", json={"name": "alice"},
                headers=h)
    c3 = addcard(client, h, pid, col, "alice's card",
                 assignees=["alice", "sweeper"])
    assert client.delete(f"/api/projects/{pid}/members/alice",
                         headers=h).status_code == 200
    assert next(x for x in board(client, h, pid)["cards"]
                if x["id"] == c3["id"])["assignees"] == ["sweeper"]

    # deleting the project takes its board, not the other project's
    p2 = mkproject(client, h, "Other")["id"]
    addcol(client, h, p2, "Keep me")
    assert client.delete(f"/api/projects/{pid}", headers=h).status_code == 200
    assert client.get(f"/api/projects/{pid}/board", headers=h).status_code == 404
    assert [x["name"] for x in board(client, h, p2)["columns"]] == ["Keep me"]


# --- validation -------------------------------------------------------------

def test_board_validation(client):
    """Malformed inputs are refused with names that say what went wrong"""
    h = mkuser(client, "validator")
    pid = mkproject(client, h, "Validate")["id"]
    col = addcol(client, h, pid, "Inbox")["id"]
    card = addcard(client, h, pid, col, "real card")

    assert client.post(f"/api/projects/{pid}/board/cards",
                       json={"column_id": col, "title": ""},
                       headers=h).status_code == 400
    assert client.post(f"/api/projects/{pid}/board/cards",
                       json={"column_id": "col_deadbeef", "title": "x"},
                       headers=h).status_code == 400
    assert client.patch(f"/api/projects/{pid}/board/cards/crd_deadbeef",
                        json={"title": "x"}, headers=h).status_code == 404
    assert client.delete(f"/api/projects/{pid}/board/cards/crd_deadbeef",
                         headers=h).status_code == 404
    assert client.post(f"/api/projects/{pid}/board/move",
                       json={"card_id": "crd_deadbeef"},
                       headers=h).status_code == 404
    assert client.post(f"/api/projects/{pid}/board/move",
                       json={"card_id": card["id"], "column_id": "col_deadbeef"},
                       headers=h).status_code == 400
    # an assignee outside the roster
    assert client.post(f"/api/projects/{pid}/board/cards",
                       json={"column_id": col, "title": "x",
                             "assignees": ["nobody"]}, headers=h).status_code == 400
    # a due date that is not a date
    assert client.post(f"/api/projects/{pid}/board/cards",
                       json={"column_id": col, "title": "x",
                             "due_date": "tomorrow"}, headers=h).status_code == 400
    # a source the project does not hold
    sid = import_md(client, h, "Elsewhere")
    assert client.post(f"/api/projects/{pid}/board/cards",
                       json={"column_id": col, "title": "x", "source_id": sid},
                       headers=h).status_code == 400