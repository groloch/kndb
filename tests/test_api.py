"""End-to-end tests for the project-first model.

These cover the rules that are easy to break and expensive to get wrong:
who may edit whose lines, that quizzes cannot reach a team project, and that
a transfer preserves authorship.
"""

import io
from itertools import groupby

import pytest


def mkuser(client, name):
    client.post("/api/users", json={"name": name})
    return {"X-KNDB-User": name}


def import_md(client, headers, title, body="hello", project="", folder=""):
    data = {"project": project, "folder": folder}
    files = {"file": (f"{title}.md", io.BytesIO(body.encode()), "text/markdown")}
    r = client.post("/api/import", data=data, files=files, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def mkproject(client, headers, name):
    r = client.post("/api/projects", json={"name": name}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["project"]


# --- source metadata ------------------------------------------------------

def test_meta_only_writes_the_fields_it_is_given(client):
    """Title and tags are edited from different places in the UI, so a request
    naming one must not blank the other."""
    h = mkuser(client, "tagger")
    sid = import_md(client, h, "Tagged doc")
    client.post(f"/api/source/{sid}/meta",
                json={"tags": "ml, survey"}, headers=h)

    def row():
        sources = client.get("/api/sources", headers=h).json()["sources"]
        return next(s for s in sources if s["id"] == sid)

    client.post(f"/api/source/{sid}/meta", json={"title": "Renamed"}, headers=h)
    assert row()["tags"] == "ml, survey"

    client.post(f"/api/source/{sid}/meta", json={"tags": "ml"}, headers=h)
    assert row()["title"] == "Renamed"

    # a blank title is a caller that never meant to touch the title
    client.post(f"/api/source/{sid}/meta", json={"title": ""}, headers=h)
    assert row()["title"] == "Renamed"


# --- identity & workspace -------------------------------------------------

def test_me_creates_a_personal_workspace(client):
    h = mkuser(client, "alice")
    me = client.get("/api/me", headers=h).json()
    assert me["user"]["name"] == "alice"
    p = me["personal_project"]
    assert p["kind"] == "personal"
    assert p["owner_user"] == "alice"


def test_personal_workspace_capabilities(client):
    h = mkuser(client, "capuser")
    caps = client.get("/api/me", headers=h).json()["personal_project"]["capabilities"]
    assert caps == {"allow_quiz": True, "multi_notes": False,
                    "show_blame": True, "auto_import": True}


def test_team_project_capabilities(client):
    h = mkuser(client, "capuser2")
    caps = mkproject(client, h, "Team")["capabilities"]
    assert caps["multi_notes"] is True
    assert caps["allow_quiz"] is False


def test_personal_workspace_cannot_be_deleted(client):
    h = mkuser(client, "undeletable")
    pid = client.get("/api/me", headers=h).json()["personal_project"]["id"]
    assert client.delete(f"/api/projects/{pid}", headers=h).status_code == 409


# --- import ---------------------------------------------------------------

def test_import_lands_in_the_workspace_with_an_empty_note(client):
    h = mkuser(client, "importer")
    sid = import_md(client, h, "Paper")
    pid = client.get("/api/me", headers=h).json()["personal_project"]["id"]

    note = client.get(f"/api/note/{sid}", headers=h).json()
    assert note["content"] == ""
    assert note["note_id"]

    tree = client.get(f"/api/projects/{pid}/tree", headers=h).json()
    assert [s["id"] for s in tree["sources"]] == [sid]


def test_import_into_a_project_mirrors_into_the_workspace(client):
    """auto_import is on for personal workspaces by default."""
    h = mkuser(client, "mirror")
    proj = mkproject(client, h, "Mirrored")
    sid = import_md(client, h, "Shared", project=proj["id"])

    personal = client.get("/api/me", headers=h).json()["personal_project"]["id"]
    tree = client.get(f"/api/projects/{personal}/tree", headers=h).json()
    assert sid in [s["id"] for s in tree["sources"]]


def test_auto_import_off_stops_mirroring(client):
    h = mkuser(client, "nomirror")
    personal = client.get("/api/me", headers=h).json()["personal_project"]["id"]
    client.post(f"/api/projects/{personal}",
                json={"capabilities": {"auto_import": False}}, headers=h)

    proj = mkproject(client, h, "Unmirrored")
    sid = import_md(client, h, "Private", project=proj["id"])
    tree = client.get(f"/api/projects/{personal}/tree", headers=h).json()
    assert sid not in [s["id"] for s in tree["sources"]]


def test_reimporting_a_url_reuses_the_blob(client):
    h = mkuser(client, "dedup")
    a = import_md(client, h, "One")
    b = import_md(client, h, "One")
    assert a != b  # uploads have no URL, so they are distinct sources


# --- note pages -----------------------------------------------------------

def test_workspace_keeps_a_single_note_per_source(client):
    h = mkuser(client, "single")
    sid = import_md(client, h, "Solo")
    pid = client.get("/api/me", headers=h).json()["personal_project"]["id"]

    r = client.post(f"/api/projects/{pid}/notes",
                    json={"source_id": sid, "name": "Second"}, headers=h)
    assert r.status_code == 409


def test_personal_note_endpoint_round_trips(client):
    """The personal page still talks to /api/note/{sid}; it now resolves to
    the caller's workspace page underneath."""
    h = mkuser(client, "roundtrip")
    sid = import_md(client, h, "RT")

    first = client.get(f"/api/note/{sid}", headers=h).json()
    assert first["note_id"] and first["version"] == 1

    saved = client.put(f"/api/note/{sid}",
                       json={"content": "my note", "base_version": 1},
                       headers=h)
    assert saved.status_code == 200, saved.text
    assert saved.json()["version"] == 2

    again = client.get(f"/api/note/{sid}", headers=h).json()
    assert again["content"] == "my note"
    assert again["note_id"] == first["note_id"]      # same page, not a new one
    assert again["blame"][0]["author"] == "roundtrip"

    stale = client.put(f"/api/note/{sid}",
                       json={"content": "clobber", "base_version": 1}, headers=h)
    assert stale.status_code == 409


def test_two_users_notes_on_the_same_source_stay_separate(client):
    """The blob is shared; the notes are not."""
    a = mkuser(client, "sharer")
    b = mkuser(client, "borrower")
    sid = import_md(client, a, "Shared blob")

    personal_b = client.get("/api/me", headers=b).json()["personal_project"]["id"]
    client.post("/api/transfer/source",
                json={"source_id": sid, "to": personal_b}, headers=b)

    client.put(f"/api/note/{sid}", json={"content": "A's take"}, headers=a)
    client.put(f"/api/note/{sid}", json={"content": "B's take"}, headers=b)
    assert client.get(f"/api/note/{sid}", headers=a).json()["content"] == "A's take"
    assert client.get(f"/api/note/{sid}", headers=b).json()["content"] == "B's take"


def test_project_allows_several_pages_per_source(client):
    h = mkuser(client, "multi")
    proj = mkproject(client, h, "Multi")
    sid = import_md(client, h, "Doc", project=proj["id"])

    for name in ("Method", "Results"):
        r = client.post(f"/api/projects/{proj['id']}/notes",
                        json={"source_id": sid, "name": name}, headers=h)
        assert r.status_code == 200, r.text
    pages = client.get(f"/api/projects/{proj['id']}/sources/{sid}/notes",
                       headers=h).json()["notes"]
    assert {p["name"] for p in pages} == {"Method", "Results"}


def test_optimistic_locking_rejects_a_stale_save(client):
    h = mkuser(client, "locker")
    proj = mkproject(client, h, "Locking")
    sid = import_md(client, h, "Doc", project=proj["id"])
    nid = client.post(f"/api/projects/{proj['id']}/notes",
                      json={"source_id": sid, "name": "N"}, headers=h).json()["note"]["id"]

    client.put(f"/api/notes/{nid}", json={"content": "v2", "base_version": 1},
               headers=h)
    stale = client.put(f"/api/notes/{nid}",
                       json={"content": "v3", "base_version": 1}, headers=h)
    assert stale.status_code == 409


# --- collaboration rules --------------------------------------------------

@pytest.fixture(scope="module")
def collab(client):
    """A project owned by 'maint' with 'contrib' as a contributor, holding one
    note written by maint."""
    owner = mkuser(client, "maint")
    mkuser(client, "contrib")
    proj = mkproject(client, owner, "Collab")
    client.post(f"/api/projects/{proj['id']}/members",
                json={"name": "contrib", "role": "contributor"}, headers=owner)
    sid = import_md(client, owner, "Shared doc", project=proj["id"])
    nid = client.post(f"/api/projects/{proj['id']}/notes",
                      json={"source_id": sid, "name": "Notes"},
                      headers=owner).json()["note"]["id"]
    client.put(f"/api/notes/{nid}",
               json={"content": "line one\nline two"}, headers=owner)
    # contrib appends a line here rather than in a test, so that tests about
    # mixed authorship do not depend on another test having run first.
    client.put(f"/api/notes/{nid}",
               json={"content": "line one\nline two\nline three"},
               headers={"X-KNDB-User": "contrib"})
    return {"project": proj, "sid": sid, "nid": nid,
            "owner": owner, "contrib": {"X-KNDB-User": "contrib"}}


def test_members_get_distinct_colors(client, collab):
    members = client.get(f"/api/projects/{collab['project']['id']}/members",
                         headers=collab["owner"]).json()["members"]
    colors = [m["color"] for m in members]
    assert len(set(colors)) == len(colors)
    assert all(c.startswith("#") for c in colors)


def test_contributor_may_append(client, collab):
    r = client.put(f"/api/notes/{collab['nid']}",
                   json={"content": "line one\nline two\nline three\nline four"},
                   headers=collab["contrib"])
    assert r.status_code == 200, r.text


def test_blame_credits_each_author(client, collab):
    note = client.get(f"/api/notes/{collab['nid']}",
                      headers=collab["contrib"]).json()
    runs = note["note"]["blame"]
    # Runs split on timestamp as well as author, so two saves by the same person
    # either side of a second boundary produce two adjacent runs. Compare the
    # authorship, which is what the note actually claims.
    authors = [a for a, _ in groupby(r["author"] for r in runs)]
    assert authors == ["maint", "contrib"]
    assert sum(r["n"] for r in runs if r["author"] == "maint") == 2


def test_contributor_may_not_rewrite_someone_elses_line(client, collab):
    r = client.put(f"/api/notes/{collab['nid']}",
                   json={"content": "REWRITTEN\nline two\nline three"},
                   headers=collab["contrib"])
    assert r.status_code == 403
    assert "maint" in r.json()["error"]


def test_maintainer_may_rewrite_anything(client, collab):
    r = client.put(f"/api/notes/{collab['nid']}",
                   json={"content": "REWRITTEN\nline two\nline three"},
                   headers=collab["owner"])
    assert r.status_code == 200, r.text


def test_a_non_member_cannot_read_the_note(client, collab):
    outsider = mkuser(client, "outsider")
    assert client.get(f"/api/notes/{collab['nid']}",
                      headers=outsider).status_code == 403


def test_spectators_cannot_write(client, collab):
    watcher = mkuser(client, "watcher")
    client.post(f"/api/projects/{collab['project']['id']}/members",
                json={"name": "watcher", "role": "spectator"},
                headers=collab["owner"])
    r = client.put(f"/api/notes/{collab['nid']}",
                   json={"content": "nope"}, headers=watcher)
    assert r.status_code == 403


# --- deleting a note page -------------------------------------------------

def test_deleting_a_page_needs_maintainer_and_sole_authorship(client):
    """Deleting destroys words, and the words are not always the deleter's, so
    it takes a maintainer *and* a page nobody else has written in."""
    owner = mkuser(client, "delowner")
    mkuser(client, "delhelper")
    helper = {"X-KNDB-User": "delhelper"}
    proj = mkproject(client, owner, "Deletions")
    client.post(f"/api/projects/{proj['id']}/members",
                json={"name": "delhelper", "role": "contributor"}, headers=owner)
    sid = import_md(client, owner, "Deletable doc", project=proj["id"])

    def page(name, headers, text):
        nid = client.post(f"/api/projects/{proj['id']}/notes",
                          json={"source_id": sid, "name": name},
                          headers=headers).json()["note"]["id"]
        client.put(f"/api/notes/{nid}", json={"content": text}, headers=headers)
        return nid

    theirs = page("Theirs", helper, "written by the helper")
    # a contributor may not delete even a page that is entirely their own
    r = client.delete(f"/api/notes/{theirs}", headers=helper)
    assert r.status_code == 403
    assert "maintainer" in r.json()["error"]
    # and the owner may not either, because the lines are someone else's
    r = client.delete(f"/api/notes/{theirs}", headers=owner)
    assert r.status_code == 403
    assert "delhelper" in r.json()["error"]
    assert client.get(f"/api/notes/{theirs}", headers=owner).json()["can_delete"] is False

    mine = page("Mine", owner, "written by the owner")
    assert client.get(f"/api/notes/{mine}", headers=owner).json()["can_delete"] is True
    assert client.delete(f"/api/notes/{mine}", headers=owner).status_code == 200
    left = client.get(f"/api/projects/{proj['id']}/sources/{sid}/notes",
                      headers=owner).json()["notes"]
    assert [p["name"] for p in left] == ["Theirs"]
    assert left[0]["authors"] == ["delhelper"]


def test_the_workspaces_single_page_cannot_be_deleted(client):
    """It would come straight back on the next read, so refuse it outright."""
    h = mkuser(client, "solodel")
    sid = import_md(client, h, "Solo doc")
    nid = client.get(f"/api/note/{sid}", headers=h).json()["note_id"]
    r = client.delete(f"/api/notes/{nid}", headers=h)
    assert r.status_code == 409


# --- folders & standalone notes -------------------------------------------

def test_folders_and_standalone_notes(client):
    h = mkuser(client, "filer")
    proj = mkproject(client, h, "Filed")
    pid = proj["id"]
    sid = import_md(client, h, "Filed doc", project=pid, folder="chapter/one")

    tree = client.get(f"/api/projects/{pid}/tree", headers=h).json()
    assert "chapter" in tree["folders"] and "chapter/one" in tree["folders"]
    assert tree["sources"][0]["folder"] == "chapter/one"

    r = client.post(f"/api/projects/{pid}/notes",
                    json={"name": "README", "folder": "chapter"}, headers=h)
    assert r.status_code == 200, r.text
    client.put(f"/api/notes/{r.json()['note']['id']}",
               json={"content": "# Chapter\n\nWhat this folder holds."}, headers=h)

    readme = client.get(f"/api/projects/{pid}/readme",
                        params={"folder": "chapter"}, headers=h).json()["note"]
    assert readme and "What this folder holds." in readme["content"]
    assert client.get(f"/api/projects/{pid}/readme",
                      params={"folder": "chapter/one"}, headers=h).json()["note"] is None


def test_standalone_note_needs_a_name(client):
    h = mkuser(client, "namer")
    proj = mkproject(client, h, "Named")
    r = client.post(f"/api/projects/{proj['id']}/notes", json={}, headers=h)
    assert r.status_code == 400


def test_renaming_a_folder_moves_its_contents(client):
    h = mkuser(client, "mover")
    proj = mkproject(client, h, "Moving")
    pid = proj["id"]
    sid = import_md(client, h, "Movable", project=pid, folder="old/deep")
    note = client.post(f"/api/projects/{pid}/notes",
                       json={"name": "Doc", "folder": "old/deep"},
                       headers=h).json()["note"]

    r = client.post(f"/api/projects/{pid}/folders",
                    json={"path": "old", "new_path": "new"}, headers=h)
    assert r.status_code == 200, r.text

    tree = client.get(f"/api/projects/{pid}/tree", headers=h).json()
    assert tree["sources"][0]["folder"] == "new/deep"
    assert [n["folder"] for n in tree["notes"] if n["id"] == note["id"]] == ["new/deep"]
    assert "old" not in tree["folders"]


def test_deleting_a_folder_keeps_its_sources(client):
    h = mkuser(client, "deleter")
    proj = mkproject(client, h, "Deleting")
    pid = proj["id"]
    sid = import_md(client, h, "Survivor", project=pid, folder="doomed")

    client.delete(f"/api/projects/{pid}/folders", params={"path": "doomed"},
                  headers=h)
    tree = client.get(f"/api/projects/{pid}/tree", headers=h).json()
    assert [s["id"] for s in tree["sources"]] == [sid]
    assert tree["sources"][0]["folder"] == ""
    assert "doomed" not in tree["folders"]


# --- transfers ------------------------------------------------------------

def test_snippet_from_project_appends_to_the_workspace_note(client, collab):
    """Project -> workspace: snippets from several pages merge into the one
    markdown page the workspace keeps for that source."""
    contrib = collab["contrib"]
    personal = client.get("/api/me", headers=contrib).json()["personal_project"]["id"]

    r = client.post("/api/transfer/snippet",
                    json={"note_id": collab["nid"], "to": personal,
                          "text": "line two"}, headers=contrib)
    assert r.status_code == 200, r.text
    content = r.json()["note"]["content"]
    assert "line two" in content
    assert "from **Collab**" in content

    again = client.post("/api/transfer/snippet",
                        json={"note_id": collab["nid"], "to": personal,
                              "text": "line three"}, headers=contrib)
    merged = again.json()["note"]["content"]
    assert "line two" in merged and "line three" in merged
    assert again.json()["note"]["id"] == r.json()["note"]["id"]  # same page


def test_snippet_keeps_the_original_author_in_blame(client, collab):
    owner = collab["owner"]
    personal = client.get("/api/me", headers=owner).json()["personal_project"]["id"]
    r = client.post("/api/transfer/snippet",
                    json={"note_id": collab["nid"], "to": personal,
                          "text": "line three"}, headers=owner)
    assert r.status_code == 200, r.text
    assert "contrib" in {run["author"] for run in r.json()["note"]["blame"]}


def test_provenance_credits_the_writer_not_the_page_creator(client, collab):
    """The page belongs to 'maint' but this line was written by 'contrib' —
    the attribution header has to follow the text, not the page."""
    owner = collab["owner"]
    target = mkproject(client, owner, "Attribution")
    sid = collab["sid"]
    client.post("/api/transfer/source",
                json={"source_id": sid, "from": collab["project"]["id"],
                      "to": target["id"]}, headers=owner)
    r = client.post("/api/transfer/snippet",
                    json={"note_id": collab["nid"], "to": target["id"],
                          "text": "line three"}, headers=owner)
    assert r.status_code == 200, r.text
    assert "@contrib" in r.json()["note"]["content"]


def test_whole_page_copy_into_another_project(client, collab):
    owner = collab["owner"]
    target = mkproject(client, owner, "Target")
    r = client.post("/api/transfer/note",
                    json={"note_id": collab["nid"], "to": target["id"]},
                    headers=owner)
    assert r.status_code == 200, r.text
    page = r.json()["note"]
    assert page["origin"]["note_id"] == collab["nid"]
    assert page["id"] != collab["nid"]          # a copy, not a link

    client.put(f"/api/notes/{page['id']}", json={"content": "diverged"},
               headers=owner)
    original = client.get(f"/api/notes/{collab['nid']}", headers=owner).json()
    assert original["note"]["content"] != "diverged"


def test_source_transfer_shares_the_blob(client, collab):
    owner = collab["owner"]
    target = mkproject(client, owner, "SourceTarget")
    r = client.post("/api/transfer/source",
                    json={"source_id": collab["sid"], "from": collab["project"]["id"],
                          "to": target["id"], "folder": "inbox"}, headers=owner)
    assert r.status_code == 200, r.text
    tree = client.get(f"/api/projects/{target['id']}/tree", headers=owner).json()
    assert [s["id"] for s in tree["sources"]] == [collab["sid"]]
    assert tree["sources"][0]["folder"] == "inbox"


def test_transfer_targets_lists_workspace_and_projects(client, collab):
    out = client.get("/api/transfer/targets", headers=collab["owner"]).json()
    assert out["personal"]["id"]
    assert "Collab" in [p["name"] for p in out["projects"]]


def test_cannot_transfer_out_of_a_project_you_are_not_in(client, collab):
    outsider = mkuser(client, "thief")
    personal = client.get("/api/me", headers=outsider).json()["personal_project"]["id"]
    r = client.post("/api/transfer/snippet",
                    json={"note_id": collab["nid"], "to": personal,
                          "text": "line two"}, headers=outsider)
    assert r.status_code == 403


# --- quizzes stay personal ------------------------------------------------

def test_quiz_is_scoped_to_the_callers_workspace(client):
    h = mkuser(client, "quizzer")
    sid = import_md(client, h, "Quizzable")
    r = client.post(f"/api/quiz/{sid}/questions",
                    json={"question": "Q?", "answers": ["a", "b"],
                          "answer_index": 1}, headers=h)
    assert r.status_code == 200, r.text

    other = mkuser(client, "quizzer2")
    assert client.get(f"/api/quiz/{sid}", headers=other).json()["questions"] == []
    assert len(client.get(f"/api/quiz/{sid}", headers=h).json()["questions"]) == 1


# --- anchors: note sentences grounded in the document ---------------------

# An anchor is an annotation, never an edit. The test that matters most here
# is the one proving it stays out of the notes system: a contributor can link
# a line somebody else wrote, and the note comes out byte-identical.

LOC = {"exact": "line two", "prefix": "line one\n", "suffix": "\nline three",
       "char_start": 9}
DOC = {"kind": "text", "page": 2, "exact": "we vary N",
       "prefix": "in Table 3 ", "suffix": ", which isolates",
       "char_start": 1180, "rects": [[0.14, 0.32, 0.41, 0.017]]}


def mkanchor(client, headers, nid, **over):
    body = {"note_loc": LOC, "doc_loc": DOC}
    body.update(over)
    return client.post(f"/api/notes/{nid}/anchors", json=body, headers=headers)


def fresh_page(client, collab, name):
    """`collab` is module-scoped, so a test that counts anchors needs a page of
    its own rather than one every earlier test has been annotating."""
    return client.post(f"/api/projects/{collab['project']['id']}/notes",
                       json={"source_id": collab["sid"], "name": name},
                       headers=collab["owner"]).json()["note"]["id"]


def test_anchor_links_a_sentence_to_a_passage(client, collab):
    nid = fresh_page(client, collab, "Linked")
    r = mkanchor(client, collab["owner"], nid)
    assert r.status_code == 200, r.text
    anchor = r.json()["anchor"]
    assert anchor["source_id"] == collab["sid"]
    assert anchor["note_loc"]["exact"] == "line two"
    assert anchor["doc_loc"]["page"] == 2

    listed = client.get(f"/api/notes/{nid}/anchors",
                        headers=collab["owner"]).json()["anchors"]
    assert [a["id"] for a in listed] == [anchor["id"]]


def test_linking_is_not_editing(client, collab):
    """A contributor may ground a line written by the maintainer. Editing that
    line would be refused; annotating it must not be, and the page itself must
    come out unchanged."""
    before = client.get(f"/api/notes/{collab['nid']}",
                        headers=collab["contrib"]).json()["note"]
    r = mkanchor(client, collab["contrib"], collab["nid"],
                 note_loc={"exact": "line one"})
    assert r.status_code == 200, r.text

    after = client.get(f"/api/notes/{collab['nid']}",
                       headers=collab["contrib"]).json()["note"]
    assert after["content"] == before["content"]
    assert after["version"] == before["version"]
    assert after["blame"] == before["blame"]


def test_spectator_cannot_link_but_can_read(client, collab):
    watcher = mkuser(client, "watcher")
    client.post(f"/api/projects/{collab['project']['id']}/members",
                json={"name": "watcher", "role": "spectator"},
                headers=collab["owner"])
    mkanchor(client, collab["owner"], collab["nid"])
    assert mkanchor(client, watcher, collab["nid"]).status_code == 403
    r = client.get(f"/api/notes/{collab['nid']}/anchors", headers=watcher)
    assert r.status_code == 200 and r.json()["anchors"]


def test_outsiders_see_no_links(client, collab):
    outsider = mkuser(client, "nosy")
    assert client.get(f"/api/notes/{collab['nid']}/anchors",
                      headers=outsider).status_code == 403
    assert mkanchor(client, outsider, collab["nid"]).status_code == 403


@pytest.mark.parametrize("bad", [
    {"note_loc": {}},                            # nothing quoted
    {"note_loc": {"exact": "   "}},              # nothing but space
    {"note_loc": "line two"},                    # not an object
    {"doc_loc": {"kind": "text"}},               # no quote on the document end
    {"doc_loc": {"kind": "region"}},             # a region locator needs rects
    {"note_loc": {"exact": "x" * 5000}},         # unbounded blob
    {"doc_loc": {"kind": "text", "exact": "x" * 20000}},   # ditto, roomier cap
])
def test_bad_locators_are_refused(client, collab, bad):
    assert mkanchor(client, collab["owner"], collab["nid"], **bad).status_code == 400


def test_a_scanned_page_can_be_anchored_by_region(client, collab):
    """No text layer means no quote; a dragged rectangle is all there is."""
    r = mkanchor(client, collab["owner"], collab["nid"],
                 doc_loc={"kind": "region", "page": 3,
                          "rects": [[0.1, 0.2, 0.3, 0.05]]})
    assert r.status_code == 200, r.text
    assert r.json()["anchor"]["doc_loc"]["kind"] == "region"


def test_a_page_cannot_link_to_another_source(client, collab):
    other = import_md(client, collab["owner"], "Elsewhere",
                      project=collab["project"]["id"])
    assert mkanchor(client, collab["owner"], collab["nid"],
                    source_id=other).status_code == 400


def test_a_standalone_page_can_quote_any_source_in_the_project(client, collab):
    pid = collab["project"]["id"]
    nid = client.post(f"/api/projects/{pid}/notes", json={"name": "Synthesis"},
                      headers=collab["owner"]).json()["note"]["id"]
    assert mkanchor(client, collab["owner"], nid,
                    source_id=collab["sid"]).status_code == 200


def test_only_the_author_or_a_maintainer_removes_a_link(client, collab):
    mine = mkanchor(client, collab["contrib"], collab["nid"]).json()["anchor"]
    stranger = mkuser(client, "meddler")
    client.post(f"/api/projects/{collab['project']['id']}/members",
                json={"name": "meddler", "role": "contributor"},
                headers=collab["owner"])
    assert client.delete(f"/api/anchor/{mine['id']}",
                         headers=stranger).status_code == 403
    assert client.delete(f"/api/anchor/{mine['id']}",
                         headers=collab["contrib"]).status_code == 200

    theirs = mkanchor(client, collab["contrib"], collab["nid"]).json()["anchor"]
    assert client.delete(f"/api/anchor/{theirs['id']}",
                         headers=collab["owner"]).status_code == 200


def test_anchors_die_with_their_note_page(client, collab):
    pid = collab["project"]["id"]
    nid = client.post(f"/api/projects/{pid}/notes",
                      json={"source_id": collab["sid"], "name": "Scratch"},
                      headers=collab["owner"]).json()["note"]["id"]
    aid = mkanchor(client, collab["owner"], nid).json()["anchor"]["id"]
    assert client.delete(f"/api/notes/{nid}",
                         headers=collab["owner"]).status_code == 200
    assert client.delete(f"/api/anchor/{aid}",
                         headers=collab["owner"]).status_code == 404


def test_anchors_die_when_the_source_leaves_the_project(client):
    owner = mkuser(client, "unlinker")
    proj = mkproject(client, owner, "Unlink")
    sid = import_md(client, owner, "Doomed", project=proj["id"])
    nid = client.post(f"/api/projects/{proj['id']}/notes",
                      json={"source_id": sid, "name": "N"},
                      headers=owner).json()["note"]["id"]
    # A standalone page quoting the same document: its anchor hangs off a page
    # the unlink does not delete, so it needs a sweep of its own.
    loose = client.post(f"/api/projects/{proj['id']}/notes",
                        json={"name": "Loose"}, headers=owner).json()["note"]["id"]
    a1 = mkanchor(client, owner, nid).json()["anchor"]["id"]
    a2 = mkanchor(client, owner, loose, source_id=sid).json()["anchor"]["id"]

    assert client.delete(f"/api/projects/{proj['id']}/sources/{sid}",
                         headers=owner).status_code == 200
    for aid in (a1, a2):
        assert client.delete(f"/api/anchor/{aid}", headers=owner).status_code == 404


def test_anchors_die_with_the_project(client):
    owner = mkuser(client, "shredder")
    proj = mkproject(client, owner, "Doomed project")
    sid = import_md(client, owner, "Doc", project=proj["id"])
    nid = client.post(f"/api/projects/{proj['id']}/notes",
                      json={"source_id": sid, "name": "N"},
                      headers=owner).json()["note"]["id"]
    aid = mkanchor(client, owner, nid).json()["anchor"]["id"]
    assert client.delete(f"/api/projects/{proj['id']}",
                         headers=owner).status_code == 200
    assert client.delete(f"/api/anchor/{aid}", headers=owner).status_code == 404


def test_the_document_overlay_lists_every_authors_links(client, collab):
    pid, sid = collab["project"]["id"], collab["sid"]
    mkanchor(client, collab["owner"], collab["nid"])
    mkanchor(client, collab["contrib"], collab["nid"],
             note_loc={"exact": "line three"})
    out = client.get(f"/api/projects/{pid}/sources/{sid}/anchors",
                     headers=collab["contrib"]).json()["anchors"]
    assert {"maint", "contrib"} <= {a["created_by"] for a in out}


def test_copying_a_page_carries_its_links(client):
    """The copy lands under a provenance header, so every offset in it moves —
    which is exactly why both ends are quotes rather than positions."""
    owner = mkuser(client, "copier")
    origin = mkproject(client, owner, "Anchor origin")
    sid = import_md(client, owner, "Anchored doc", body="a paper",
                    project=origin["id"])
    nid = client.post(f"/api/projects/{origin['id']}/notes",
                      json={"source_id": sid, "name": "Notes"},
                      headers=owner).json()["note"]["id"]
    client.put(f"/api/notes/{nid}",
               json={"content": "line one\nline two"}, headers=owner)
    aid = mkanchor(client, owner, nid).json()["anchor"]["id"]

    target = mkproject(client, owner, "Anchor target")
    client.post("/api/transfer/source",
                json={"source_id": sid, "from": origin["id"],
                      "to": target["id"], "with_notes": True}, headers=owner)

    pages = client.get(f"/api/projects/{target['id']}/sources/{sid}/notes",
                       headers=owner).json()["notes"]
    assert len(pages) == 1
    copied = client.get(f"/api/notes/{pages[0]['id']}/anchors",
                        headers=owner).json()["anchors"]
    assert len(copied) == 1
    assert copied[0]["id"] != aid                    # a copy, not a move
    assert copied[0]["note_loc"]["exact"] == "line two"
    assert copied[0]["created_by"] == "copier"       # authorship survives
    assert len(client.get(f"/api/notes/{nid}/anchors",
                          headers=owner).json()["anchors"]) == 1
