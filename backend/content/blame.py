"""Line authorship of a note page — what the blame gutter colors.
Pure: no I/O, no clock, the caller supplies every timestamp.
Authorship travels run-length encoded, [{"author", "ts", "n"}, ...], one run
per stretch of consecutive lines by the same author at the same moment
"""

import difflib


__all__ = [
    "split_lines", "expand", "compress", "seed", "rebase", "foreign_edits",
    "authors", "reattribute",
]


def split_lines(content: str) -> list:
    """Lines of a note page, the unit blame is counted in.
    Empty content still counts as one line
    """
    return (content or "").split("\n")

def expand(rle: list, n_lines: int | None = None) -> list:
    """Runs back to one entry per line, padded with unattributed entries or
    truncated to reach n_lines, its natural length when n_lines is None.
    Lines of a run share one entry object, so treat the result as read-only
    """
    out = []
    for run in rle or []:
        entry = {"author": run.get("author", ""), "ts": run.get("ts", "")}
        out.extend([entry] * max(0, int(run.get("n", 0))))
    if n_lines is None:
        return out
    if len(out) > n_lines:
        del out[n_lines:]
    while len(out) < n_lines:
        out.append({"author": "", "ts": ""})
    return out

def compress(per_line: list) -> list:
    """Per-line entries back to runs, neighbours with the same author and
    timestamp merged
    """
    out: list = []
    for e in per_line:
        author, ts = e.get("author", ""), e.get("ts", "")
        if out and out[-1]["author"] == author and out[-1]["ts"] == ts:
            out[-1]["n"] += 1
        else:
            out.append({"author": author, "ts": ts, "n": 1})
    return out

def seed(content: str, author: str, ts: str) -> list:
    """Blame of freshly created content: every line to one author
    """
    n = len(split_lines(content))
    return [{"author": author, "ts": ts, "n": n}] if n else []

def _opcodes(old_lines: list, new_lines: list):
    # autojunk drops lines that repeat, silently reattributing them
    return difflib.SequenceMatcher(a=old_lines, b=new_lines,
                                   autojunk=False).get_opcodes()

def rebase(old_content: str, new_content: str, old_rle: list,
           author: str, ts: str) -> list:
    """Line authorship after an edit, re-derived from the diff.
    Kept lines keep their author, replaced and inserted lines go to the saver
    """
    old_lines = split_lines(old_content)
    new_lines = split_lines(new_content)
    old_blame = expand(old_rle, len(old_lines))
    mine = {"author": author, "ts": ts}

    per_line: list = []
    for tag, i1, i2, j1, j2 in _opcodes(old_lines, new_lines):
        if tag == "equal":
            per_line.extend(old_blame[i1:i2])
        elif tag in ("replace", "insert"):
            per_line.extend([mine] * (j2 - j1))
    return compress(per_line)

def foreign_edits(old_content: str, new_content: str, old_rle: list,
                  author: str) -> list:
    """Old line numbers, 1-based, this author rewrites or deletes without
    having written them.
    Lines nobody is credited with are free to take.
    Walks the same opcodes as rebase, so permission and attribution cannot
    disagree
    """
    old_lines = split_lines(old_content)
    old_blame = expand(old_rle, len(old_lines))

    hits = []
    for tag, i1, i2, _j1, _j2 in _opcodes(old_lines, split_lines(new_content)):
        if tag not in ("replace", "delete"):
            continue
        for i in range(i1, i2):
            owner = old_blame[i]["author"]
            if owner and owner != author:
                hits.append(i + 1)
    return hits

def authors(rle: list) -> set:
    """Everyone credited in a blame record, unattributed lines excluded
    """
    return {r.get("author", "") for r in (rle or []) if r.get("author")}

def reattribute(rle: list, mapping: dict) -> list:
    """Rewrites authors through a mapping, names absent from it left alone.
    Recompresses, since remapping can leave neighbouring runs identical
    """
    out = [{"author": mapping.get(r.get("author", ""), r.get("author", "")),
            "ts": r.get("ts", ""), "n": r.get("n", 0)} for r in (rle or [])]
    return compress(expand(out))
