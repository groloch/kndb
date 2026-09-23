"""The agent's per-session memory — in memory only, per (project, user).

The per-session precursor of ideas.md §3a: each (project, user) pair gets one
conversation, kept for as long as the process lives and dropped on restart.
A session carries two things:

- ``chat`` — the message list the next run continues from (everything after
  the system prompt, in the exact shape the loop built it: user, assistant
  with its tool_calls, tool answers). Capped to the last few turns so a long
  afternoon cannot push the context off the model's window.
- ``log`` — the display events (tokens, tool calls) the run streamed, so a
  reloaded page can redraw the thread as it was.

A failed run writes nothing: partial output from a dead model never becomes
history. Sessions idle past the TTL are pruned on the next touch, and the
store holds at most MAX_SESSIONS of them. A per-session lock keeps one user's
runs from interleaving; a second request while one is streaming is refused
with an error event, not queued.
"""

import asyncio
import time

MAX_TURNS = 12        # user-initiated exchanges kept in the history
MAX_CHARS = 60_000    # total character budget for the stored history
MESSAGE_CAP = 4_000   # per-message cap applied when a turn enters history
MAX_LOG = 400         # display events kept for replay
TTL = 2 * 3600        # seconds a session may sit untouched before pruning
MAX_SESSIONS = 200    # beyond this, the least recently touched are dropped


class Session:
    """One (project, user) conversation: loop history, display log, a lock
    serializing its runs, and the last-touch time the pruning reads
    """
    __slots__ = ("chat", "log", "touched", "lock")

    def __init__(self):
        self.chat: list = []
        self.log: list = []
        self.touched = time.time()
        self.lock = asyncio.Lock()


_sessions: dict = {}


def _prune() -> None:
    """Drops expired sessions, and the oldest ones past the store's cap
    """
    now = time.time()
    stale = [k for k, s in _sessions.items() if now - s.touched > TTL]
    for k in stale:
        del _sessions[k]
    if len(_sessions) > MAX_SESSIONS:
        keep = sorted(_sessions, key=lambda k: _sessions[k].touched)[-MAX_SESSIONS:]
        for k in list(_sessions):
            if k not in keep:
                del _sessions[k]


def get(pid: str, user: str) -> Session:
    """The pair's session, created empty on first sight
    """
    _prune()
    key = (pid, user)
    sess = _sessions.get(key)
    if sess is None:
        sess = _sessions[key] = Session()
    sess.touched = time.time()
    return sess


def clear(pid: str, user: str) -> None:
    """Forgets the pair's conversation — the New conversation button
    """
    _sessions.pop((pid, user), None)


def remember(sess: Session, new_messages: list, events: list) -> None:
    """Folds one finished run into the session: its messages continue the
    history, its events continue the display log
    """
    sess.chat = _capped(sess.chat + list(new_messages))
    sess.log = (sess.log + list(events))[-MAX_LOG:]
    sess.touched = time.time()


def replay(pid: str, user: str) -> list:
    """The display log, for a page redrawing its thread after a reload
    """
    sess = _sessions.get((pid, user))
    if sess is None:
        return []
    sess.touched = time.time()
    return list(sess.log)


def _turn_starts(chat: list) -> list:
    """Indexes of the messages that open a turn — every user message
    """
    return [i for i, m in enumerate(chat) if m.get("role") == "user"]


def _capped(chat: list) -> list:
    """The history as the next run may carry it: the last MAX_TURNS turns,
    under MAX_CHARS characters (whole oldest turns dropped until it fits),
    each stored message's bulky parts trimmed to MESSAGE_CAP
    """
    starts = _turn_starts(chat)
    if len(starts) > MAX_TURNS:
        chat = chat[starts[len(starts) - MAX_TURNS]:]

    def length(c: list) -> int:
        return sum(len(m.get("content") or "") for m in c)

    while chat and length(chat) > MAX_CHARS:
        first = _turn_starts(chat)
        if not first:
            break
        nxt = first[1] if len(first) > 1 else len(chat)
        chat = chat[nxt:]

    out = []
    for m in chat:
        content = m.get("content")
        if isinstance(content, str) and len(content) > MESSAGE_CAP:
            m = dict(m, content=content[:MESSAGE_CAP]
                     + f"\n[… truncated, {len(content) - MESSAGE_CAP} chars dropped]")
        reasoning = m.get("reasoning_content")
        if isinstance(reasoning, str) and len(reasoning) > MESSAGE_CAP:
            m = dict(m, reasoning_content=reasoning[:MESSAGE_CAP] + " […]")
        out.append(m)
    return out
