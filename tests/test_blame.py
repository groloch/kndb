"""Blame engine tests.

Blame decides both who wrote a line and whether a save is allowed, so the
diff behaviour is pinned down here rather than discovered in production.
"""

import pytest

from backend.content import blame


T0, T1 = "2026-01-01T00:00:00", "2026-01-02T00:00:00"


def authors_of(rle, content):
    """Per-line author list, the shape the gutter renders."""
    return [e["author"] for e in blame.expand(rle, len(blame.split_lines(content)))]


# --- encoding -------------------------------------------------------------

def test_seed_attributes_every_line():
    c = "a\nb\nc"
    assert authors_of(blame.seed(c, "alice", T0), c) == ["alice"] * 3


def test_empty_content_is_one_line():
    assert blame.split_lines("") == [""]
    assert authors_of(blame.seed("", "alice", T0), "") == ["alice"]


def test_compress_merges_only_identical_runs():
    per_line = [
        {"author": "a", "ts": T0}, {"author": "a", "ts": T0},
        {"author": "a", "ts": T1},            # same author, later edit
        {"author": "b", "ts": T1},
    ]
    assert [r["n"] for r in blame.compress(per_line)] == [2, 1, 1]


def test_expand_pads_and_truncates_desynced_blame():
    rle = [{"author": "a", "ts": T0, "n": 2}]
    assert len(blame.expand(rle, 5)) == 5
    assert blame.expand(rle, 5)[4]["author"] == ""
    assert len(blame.expand(rle, 1)) == 1


def test_roundtrip():
    c = "a\nb\nc\nd"
    rle = blame.seed(c, "alice", T0)
    assert blame.compress(blame.expand(rle)) == rle


# --- rebase ---------------------------------------------------------------

def test_append_leaves_existing_lines_untouched():
    old = "one\ntwo"
    new = "one\ntwo\nthree"
    rle = blame.rebase(old, new, blame.seed(old, "alice", T0), "bob", T1)
    assert authors_of(rle, new) == ["alice", "alice", "bob"]


def test_insert_in_the_middle():
    old = "one\nthree"
    new = "one\ntwo\nthree"
    rle = blame.rebase(old, new, blame.seed(old, "alice", T0), "bob", T1)
    assert authors_of(rle, new) == ["alice", "bob", "alice"]


def test_rewriting_a_line_takes_it_over():
    old = "one\ntwo\nthree"
    new = "one\nTWO\nthree"
    rle = blame.rebase(old, new, blame.seed(old, "alice", T0), "bob", T1)
    assert authors_of(rle, new) == ["alice", "bob", "alice"]


def test_deleting_a_line_drops_its_blame():
    old = "one\ntwo\nthree"
    new = "one\nthree"
    rle = blame.rebase(old, new, blame.seed(old, "alice", T0), "bob", T1)
    assert authors_of(rle, new) == ["alice", "alice"]


def test_moved_lines_keep_their_author():
    old = "alpha\nbeta\ngamma\ndelta"
    new = "gamma\ndelta\nalpha\nbeta"
    per_line = [{"author": "alice", "ts": T0}] * 2 + [{"author": "carol", "ts": T0}] * 2
    rle = blame.rebase(old, new, blame.compress(per_line), "bob", T1)
    # one of the two blocks is matched by the diff and survives; the other is
    # re-typed. Whichever way it lands, carol must not be credited to alice.
    got = authors_of(rle, new)
    assert got[:2] == ["carol", "carol"] or got[2:] == ["alice", "alice"]


def test_blank_lines_do_not_get_junked_in_long_notes():
    """SequenceMatcher's autojunk heuristic kicks in above 200 elements and
    would refuse to match frequently-repeated lines — silently reattributing
    untouched paragraphs. This is the regression that guards it."""
    old = "\n".join(f"line {i}\n" for i in range(400))          # 800 lines, half blank
    new = old + "appended\n"
    rle = blame.rebase(old, new, blame.seed(old, "alice", T0), "bob", T1)
    got = authors_of(rle, new)
    assert set(got[:-2]) == {"alice"}
    assert got[-2] == "bob"


def test_rebase_is_stable_when_nothing_changed():
    c = "one\ntwo"
    rle = blame.seed(c, "alice", T0)
    assert blame.rebase(c, c, rle, "bob", T1) == rle


def test_full_rewrite_takes_everything():
    old = "one\ntwo"
    new = "completely\ndifferent\ntext"
    rle = blame.rebase(old, new, blame.seed(old, "alice", T0), "bob", T1)
    assert authors_of(rle, new) == ["bob"] * 3


# --- permissions ----------------------------------------------------------

def test_appending_touches_nobody():
    old = "one\ntwo"
    assert blame.foreign_edits(old, old + "\nthree",
                               blame.seed(old, "alice", T0), "bob") == []


def test_editing_own_lines_is_allowed():
    old = "one\ntwo"
    assert blame.foreign_edits(old, "one\nTWO",
                               blame.seed(old, "bob", T0), "bob") == []


def test_rewriting_someone_elses_line_is_reported():
    old = "one\ntwo\nthree"
    assert blame.foreign_edits(old, "one\nTWO\nthree",
                               blame.seed(old, "alice", T0), "bob") == [2]


def test_deleting_someone_elses_line_is_reported():
    old = "one\ntwo\nthree"
    assert blame.foreign_edits(old, "one\nthree",
                               blame.seed(old, "alice", T0), "bob") == [2]


def test_line_numbers_are_1_based_and_refer_to_the_old_content():
    old = "a\nb\nc\nd\ne"
    got = blame.foreign_edits(old, "a\nb\nc", blame.seed(old, "alice", T0), "bob")
    assert got == [4, 5]


def test_mixed_ownership_only_reports_foreign_lines():
    old = "mine\ntheirs"
    rle = blame.compress([{"author": "bob", "ts": T0}, {"author": "alice", "ts": T0}])
    assert blame.foreign_edits(old, "MINE\ntheirs", rle, "bob") == []
    assert blame.foreign_edits(old, "mine\nTHEIRS", rle, "bob") == [2]


def test_unattributed_lines_are_free_to_edit():
    """Migrated or hand-inserted content with no author must not lock the
    page — an empty author is nobody's, so anyone may edit it."""
    old = "orphan"
    assert blame.foreign_edits(old, "adopted", [], "bob") == []


# --- transfers ------------------------------------------------------------

def test_authors_lists_contributors():
    rle = blame.compress([{"author": "a", "ts": T0}, {"author": "b", "ts": T0},
                          {"author": "", "ts": ""}])
    assert blame.authors(rle) == {"a", "b"}


def test_reattribute_remaps_and_recompresses():
    c = "one\ntwo"
    rle = blame.compress([{"author": "alice", "ts": T0}, {"author": "bob", "ts": T0}])
    out = blame.reattribute(rle, {"alice": "ext", "bob": "ext"})
    assert authors_of(out, c) == ["ext", "ext"]
    assert len(out) == 1  # merged into a single run


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
