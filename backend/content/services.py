"""What the app does with a source once it is stored: quizzes, summaries,
spaced repetition.
Reads the note pages and the document text, streams the model's answer, and
writes the result back through the quiz and note stores
"""

import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

from ..data import notes as note_store
from ..data import quiz as quiz_store
from ..data import store, projects
from ..integrations import llm
from ..content import sessions
from ..content import tool_guard
from ..content import agent
from ..content.agent import (parse_pythonic_toolcall,
                             normalize_native_toolcall)


_INTERVALS = [0, 1, 2, 4, 7, 15, 30, 60]
_LENS = {"short": "~150 words", "medium": "~400 words", "detailed": "~900 words"}


def _now() -> str:
    """UTC stamp, for anything compared against a schedule
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _local_now() -> str:
    """Local wall-clock stamp, for anything a human reads
    """
    return time.strftime("%Y-%m-%dT%H:%M:%S")

async def material_for(scope: str, row: dict, notes_text: str = ""):
    """(notes_text, document_text) for the requested scope, whichever falls
    outside it empty.
    Scope is "notes", "document" or "both"
    """
    notes, doc = "", ""
    if scope in ("notes", "both"):
        notes = notes_text or ""
    if scope in ("document", "both"):
        doc = await document_text(row)
    return notes, doc

async def document_text(row: dict) -> str:
    """The source document as plain text, "" for a type with no extractor.
    Prefers the .txt sidecar the fetcher may have cached, cleaner than
    anything re-extracted from the blob here.
    An unparseable PDF yields a bracketed note rather than raising, while a
    missing or unreadable file still raises
    """
    stype, path = row["source_type"], row["source_path"]

    def _sidecar():
        with open(store.text_sidecar(path), encoding="utf-8") as f:
            return f.read()

    def _md():
        with open(path, encoding="utf-8") as f:
            return f.read()

    def _html():
        from bs4 import BeautifulSoup

        with open(path, encoding="utf-8", errors="replace") as f:
            soup = BeautifulSoup(f.read(), "html.parser")
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if href.startswith(("http://", "https://")):
                a.insert_after(" " + href)
        return soup.get_text("\n", strip=True)

    def _pdf():
        try:
            from pypdf import PdfReader

            reader = PdfReader(path)
            return "\n".join((p.extract_text() or "") for p in reader.pages)
        except Exception as e:  # noqa: BLE001
            return f"[could not extract text from this PDF: {e}]"

    if os.path.exists(store.text_sidecar(path)):
        text = await asyncio.to_thread(_sidecar)
        if text.strip():
            return text
    if stype == "md":
        return await asyncio.to_thread(_md)
    if stype in ("html", "html+css"):
        return await asyncio.to_thread(_html)
    if stype == "pdf":
        return await asyncio.to_thread(_pdf)
    return ""

def _check_material(scope: str, notes: str, doc: str) -> None:
    """Raises ValueError, message meant for the user, when the chosen scope
    has nothing to work from
    """
    if scope == "notes" and not notes.strip():
        raise ValueError("There are no personal notes for this source yet — "
                         "write some notes first, or pick a different scope.")
    if scope == "document" and not doc.strip():
        raise ValueError("Could not extract text from the document — pick "
                         "'Notes only', or import a document with text.")
    if scope == "both" and not notes.strip() and not doc.strip():
        raise ValueError("Both the notes and the document are empty for this source.")

def _normalize_questions(data):
    """Model output into the stored question shape, whatever it gave back.
    Takes a bare list or a {"questions"}/{"quiz"} wrapper, answers as strings,
    dicts or a {text: correct} mapping, and settles on the first answer when
    none is flagged correct.
    Caps at 40, drops questions left with fewer than two answers, and raises
    ValueError when nothing usable survives
    """
    if isinstance(data, dict):
        data = data.get("questions") or data.get("quiz") or []
    if not isinstance(data, list):
        raise ValueError("The model did not return a list of questions.")
    out = []
    used = set()
    for i, q in enumerate(data[:40]):
        if not isinstance(q, dict):
            continue
        question = str(q.get("question") or q.get("prompt") or "").strip()
        if not question:
            continue
        answers_raw = q.get("answers") or []
        if isinstance(answers_raw, dict):  # {"text": bool} mapping
            answers_raw = list(answers_raw.items())
        answers, correct = [], None
        for idx, a in enumerate(answers_raw):
            if isinstance(a, dict):
                txt = str(a.get("text") or a.get("answer") or "")
                if a.get("correct") is True:
                    correct = idx
            elif isinstance(a, tuple):
                txt = str(a[0])
                if a[1] is True:
                    correct = idx
            else:
                txt = str(a)
            txt = txt.strip()
            if txt:
                answers.append(txt)
        if correct is None and isinstance(q.get("answer_index"), int):
            correct = q.get("answer_index")
        if correct is None:
            correct = 0
        try:
            correct = int(correct)
        except (TypeError, ValueError):
            correct = 0
        if len(answers) < 2 or not (0 <= correct < len(answers)):
            continue
        qid = str(q.get("id") or f"q{i + 1}")
        while qid in used:
            qid += "_"
        used.add(qid)
        out.append({"id": qid, "question": question,
                    "answers": answers, "answer_index": correct})
    if not out:
        raise ValueError("The model produced no usable questions "
                         "(its response may have been malformed).")
    return out

def _init_question_stats(stats: dict, questions: list) -> None:
    """Gives every question a blank stats row, leaving rows that exist alone.
    Rows are keyed by question id, so a regenerated quiz keeps the history of
    the ids it happens to reuse
    """
    stats["num_questions"] = len(questions)
    stats["date_last_modified"] = _local_now()
    base = {"times_played": 0, "times_successful": 0, "box": 0,
            "next_review": "", "last_played": ""}
    for q in questions:
        stats["questions"].setdefault(q["id"], dict(base))

async def stream_quiz(pid: str, sid: str, scope: str = "both", num_questions: int = 5,
                      difficulty: str = "medium", language: str = "English"):
    """Builds a quiz from the source material, yielding token events as the
    model writes.
    num_questions is clamped to 1..20, and the material is truncated before it
    reaches the prompt.
    Ends on a "done" event or a single "error" one — nothing raises, the
    caller is an SSE stream.
    Quiz and stats are written only once the streamed JSON parses
    """
    try:
        row = await store.get_source(sid)
        if not row:
            raise ValueError("source not found")
        num_questions = max(1, min(int(num_questions), 20))
        notes, doc = await material_for(scope, row,
                                        await note_store.combined_text(pid, sid))
        _check_material(scope, notes, doc)

        instructions = (f"Create exactly {num_questions} multiple-choice questions "
                        f"with 4 answers each. Difficulty: {difficulty}. "
                        f"All questions and answers must be written in {language}.")
        system = llm.load_prompt(
            "quiz_creation",
            instructions=instructions,
            num_questions=num_questions,
            difficulty=difficulty,
            language=language,
            notes=notes[:6000] or "(no notes provided)",
            document=doc[:9000] or "(no document text provided)",
        )
        text = ""
        chat = [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': "Generate the quiz based on the material above."}
        ]
        async for token in llm.stream_chat(
            chat, max_tokens=2048, temperature=0.5,
        ):
            text += token
            yield {"type": "token", "text": token}

        questions = _normalize_questions(llm.extract_json(text))
        await quiz_store.save_quiz(pid, sid, {
            "source_id": sid, "updated_at": _local_now(), "questions": questions,
        })
        stats = await quiz_store.read_stats(pid, sid) or quiz_store.new_stats(
            row["title"], row["source_type"], row["tags"], row["url"])
        stats.setdefault("questions", {})
        _init_question_stats(stats, questions)
        await quiz_store.save_stats(pid, sid, stats)
        yield {"type": "done", "questions": len(questions)}
    except Exception as e:
        yield {"type": "error", "error": str(e)}

async def record_answer(pid: str, sid: str, qid: str, success: bool) -> dict:
    """Records one answer and reschedules the question, Leitner style.
    A hit moves it up a box, a miss drops it back to box 0, and the box picks
    how many days until it is due again.
    Creates the stats row when the question has none
    """
    stats = await quiz_store.read_stats(pid, sid)
    if not stats:
        stats = {"num_questions": 0, "questions": {}, "date_last_modified": _local_now(),
                 "date_added": _local_now()}
    q = stats.setdefault("questions", {}).get(qid)
    if q is None:
        q = {"times_played": 0, "times_successful": 0, "box": 0,
             "next_review": "", "last_played": ""}
        stats["questions"][qid] = q
    q["times_played"] = q.get("times_played", 0) + 1
    if success:
        q["times_successful"] = q.get("times_successful", 0) + 1
        q["box"] = min(q.get("box", 0) + 1, len(_INTERVALS) - 1)
    else:
        q["box"] = 0
    review = datetime.now(timezone.utc) + timedelta(days=_INTERVALS[q["box"]])
    q["next_review"] = review.strftime("%Y-%m-%dT%H:%M:%SZ")
    q["last_played"] = _now()
    stats["date_last_modified"] = _local_now()
    await quiz_store.save_stats(pid, sid, stats)
    return q

def _next_qid(questions: list) -> str:
    """First free qN id in a quiz
    """
    used = {str(q.get("id")) for q in questions}
    n = 1
    while f"q{n}" in used:
        n += 1
    return f"q{n}"

def _normalize_manual_question(question, answers, answer_index) -> dict:
    """Validates a hand-written question, raising ValueError with a message
    meant for the user.
    Blank answers are dropped first, so an index counted against the list as
    sent can end up out of range
    """
    question = (question or "").strip()
    answers = [str(a).strip() for a in (answers or [])]
    answers = [a for a in answers if a]
    if not question:
        raise ValueError("Question text cannot be empty.")
    if len(answers) < 2:
        raise ValueError("Provide at least two non-empty answers.")
    try:
        answer_index = int(answer_index)
    except (TypeError, ValueError):
        answer_index = 0
    if not (0 <= answer_index < len(answers)):
        raise ValueError("The correct-answer index is out of range.")
    return {"question": question, "answers": answers, "answer_index": answer_index}

async def add_quiz_question(pid: str, sid: str, question: str, answers: list,
                            answer_index: int = 0) -> dict:
    """Appends a question to the quiz and opens a blank stats row for it
    """
    quiz = await quiz_store.read_quiz(pid, sid)
    questions = quiz.setdefault("questions", [])
    payload = _normalize_manual_question(question, answers, answer_index)
    qid = _next_qid(questions)
    q = {"id": qid, **payload}
    questions.append(q)
    quiz["updated_at"] = _local_now()
    await quiz_store.save_quiz(pid, sid, quiz)

    stats = await quiz_store.read_stats(pid, sid)
    if not stats:
        stats = {"num_questions": 0, "questions": {}, "date_added": _local_now(),
                 "date_last_modified": _local_now()}
    stats.setdefault("questions", {})[qid] = {
        "times_played": 0, "times_successful": 0, "box": 0,
        "next_review": "", "last_played": ""}
    stats["num_questions"] = len(questions)
    stats["date_last_modified"] = _local_now()
    await quiz_store.save_stats(pid, sid, stats)
    return q

async def update_quiz_question(pid: str, sid: str, qid: str, question=None,
                               answers=None, answer_index=None) -> dict:
    """Edits one question in place, the fields left None keeping their value.
    Raises KeyError when the id is not in the quiz.
    Stats are untouched, so the question keeps its box and its history
    """
    quiz = await quiz_store.read_quiz(pid, sid)
    questions = quiz.get("questions", [])
    q = next((x for x in questions if x.get("id") == qid), None)
    if q is None:
        raise KeyError(f"question {qid!r} not found in quiz")
    payload = _normalize_manual_question(
        question if question is not None else q.get("question", ""),
        answers if answers is not None else q.get("answers", []),
        answer_index if answer_index is not None else q.get("answer_index", 0),
    )
    q.update(payload)
    quiz["updated_at"] = _local_now()
    await quiz_store.save_quiz(pid, sid, quiz)
    return q

async def delete_quiz_question(pid: str, sid: str, qid: str) -> None:
    """Removes a question and the stats row that went with it.
    Raises KeyError when the id is not in the quiz
    """
    quiz = await quiz_store.read_quiz(pid, sid)
    questions = quiz.get("questions", [])
    remaining = [q for q in questions if q.get("id") != qid]
    if len(remaining) == len(questions):
        raise KeyError(f"question {qid!r} not found in quiz")
    quiz["questions"] = remaining
    quiz["updated_at"] = _local_now()
    await quiz_store.save_quiz(pid, sid, quiz)

    stats = await quiz_store.read_stats(pid, sid)
    if stats:
        stats.get("questions", {}).pop(qid, None)
        stats["num_questions"] = len(remaining)
        stats["date_last_modified"] = _local_now()
        await quiz_store.save_stats(pid, sid, stats)

async def quiz_order(pid: str, sid: str) -> list:
    """Questions in review order: due first, then the weakest, then the lowest
    box.
    A question never played counts as due
    """
    quiz = await quiz_store.read_quiz(pid, sid)
    stats = await quiz_store.read_stats(pid, sid)
    now = _now()

    def key(q):
        st = stats.get("questions", {}).get(q["id"], {})
        nr = st.get("next_review") or ""
        due = (not nr) or nr <= now
        played = st.get("times_played", 0)
        rate = st.get("times_successful", 0) / max(played, 1)
        return (0 if due else 1, rate, st.get("box", 0), q.get("id", ""))

    return sorted(quiz.get("questions", []), key=key)

async def stream_summarize(pid: str, sid: str, note_id: str, author: str,
                           length: str = "medium", language: str = "English"):
    """Summarizes the document, yielding token events, then appends the result
    to the note page under a dated heading.
    The notes are not read, only the document, truncated to 10k characters.
    Ends on a "done" event or a single "error" one, nothing raises
    """
    try:
        row = await store.get_source(sid)
        if not row:
            raise ValueError("source not found")
        page = await note_store.get(note_id)
        if not page:
            raise ValueError("note page not found")
        doc = await document_text(row)
        if not doc.strip():
            raise ValueError("Could not extract any text from this document.")

        system = llm.load_prompt(
            "summarization",
            instructions=f"Length: {_LENS.get(length, length)}. Write in {language}.",
        )
        user = (
            "Source document:\n"
            "=====\n"
            f"{doc[:10000]}\n"
            "=====\n\n"
            "Write the summary now."
        )
        text = ""
        chat = [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': user}
        ]
        async for token in llm.stream_chat(
            chat, temperature=0.8,
        ):
            text += token
            yield {"type": "token", "text": token}

        summary = text.strip()
        await note_store.append(
            note_id, summary, author=author,
            header=f"## Summary — {_local_now()}")
        await quiz_store.touch(pid, sid)
        yield {"type": "done", "chars": len(summary)}
    except Exception as e:
        yield {"type": "error", "error": str(e)}

_SENTINEL_OPEN = "<|tool_call_start|>"
_SENTINEL_CLOSE = "<|tool_call_end|>"
_SENTINEL_RE = re.compile(
    re.escape(_SENTINEL_OPEN) + r"(.*?)" + re.escape(_SENTINEL_CLOSE), re.S)


async def _run_tool(functions: dict, name: str, args: dict) -> str:
    """The tool's answer, or a refusal string the model can act on — a call
    that names no known tool or crashes is material, not the end of the run
    """
    if name not in functions:
        return f"unknown tool: {name}"
    try:
        return await functions[name](**args)
    except Exception as e:  # noqa: BLE001
        return f"tool {name} failed: {e}"


def _emit(role: str, text: str) -> tuple:
    """A token event, as the loop streams it out
    """
    return ("event", {"type": "token",
                      "role": "assistant" if role == "answer" else "assistant-trace",
                      "text": text})


async def _guarded_run(guard: tool_guard.ToolGuard, security: dict,
                       functions: dict, name: str, args: dict):
    """One tool call through the guard. Yields ("event", ev) for the
    approval prompt and its answer when the tool's level asks for one, then
    one final ("result", …) — the tool's answer or the refusal string that
    stands in for it. A denied call (by policy or by the user) never runs;
    the model just reads the refusal, the way it reads any other failed call.
    The wait holds no LLM connection: calls run between the model's turns,
    never during one
    """
    if name not in functions:
        # before any guard: an unknown tool is refused on the spot, it never
        # triggers a prompt
        yield ("result", await _run_tool(functions, name, args))
        return
    outcome, payload = guard.begin(name, security.get(name, "admin"), args)
    if outcome == "deny":
        yield ("result", payload)
        return
    if outcome == "prompt":
        yield ("event", {"type": "approval_request", "request_id": payload,
                         "tool": name, "args": args})
        decision = await guard.wait(payload)
        yield ("event", {"type": "approval_result", "request_id": payload,
                         "decision": decision})
        if decision != "allow":
            yield ("result", f"tool call refused: {name} was denied by the user")
            return
    yield ("result", await _run_tool(functions, name, args))


async def _text_turn(chat: list, functions: dict, guard: tool_guard.ToolGuard,
                     security: dict):
    """One agent turn under the text-sentinel contract: the server parses no
    tool calls, the model writes them pythonically between the sentinels, and
    the reasoning ` thinking`/`<thinking>` block rides inline in the answer.
    Yields ("event", ev) as the tokens land — a sentinel-aware splitter
    swallows the calls live, so only trace and answer ever stream — then one
    ("turn", assistant, tool_msgs) record: what to append to the conversation
    and one tool message per call made. The calls themselves run and stream
    once the turn closes, their spans fully in hand
    """
    splitter = llm.ThinkingSplitter(drops=[(_SENTINEL_OPEN, _SENTINEL_CLOSE)])
    raw_parts = []
    async for part in llm.stream_agent_chat(chat):
        text = part.get("text") or ""
        if not text:
            continue
        raw_parts.append(text)
        for role, piece in splitter.feed(text):
            yield _emit(role, piece)
    for role, piece in splitter.spill():
        yield _emit(role, piece)

    raw = "".join(raw_parts)
    tool_msgs = []
    for call_str in _SENTINEL_RE.findall(raw):
        try:
            name, args = parse_pythonic_toolcall(call_str)
        except Exception as e:  # noqa: BLE001
            result = f"could not parse tool call: {e}"
            yield ("event", {"type": "toolcall", "tool": "?", "args": {},
                             "result": result})
        else:
            result = None
            async for item in _guarded_run(guard, security, functions, name, args):
                if item[0] == "event":
                    yield ("event", item[1])
                else:
                    result = item[1]
            yield ("event", {"type": "toolcall", "tool": name, "args": args,
                             "result": result})
        tool_msgs.append({"role": "tool", "content": result})
    yield ("turn", {"role": "assistant", "content": raw}, tool_msgs)


async def _native_turn(chat: list, tools: list, functions: dict,
                       guard: tool_guard.ToolGuard, security: dict):
    """One agent turn over the server's native tool-call contract: the tools
    array goes in the request, tool_calls come back structurally, and the
    reasoning arrives in reasoning_content or as a ` thinking` block inline
    in the content. Tokens and tool results stream as they land, yielding
    like _text_turn
    """
    raw_parts = []
    reasoning = []
    calls = []
    splitter = llm.ThinkingSplitter()
    async for part in llm.stream_agent_chat(chat, tools=tools):
        kind = part["type"]
        if kind == "reasoning":
            reasoning.append(part["text"])
            yield ("event", {"type": "token", "role": "assistant-trace",
                             "text": part["text"]})
        elif kind == "content":
            text = part["text"]
            raw_parts.append(text)
            for role, piece in splitter.feed(text):
                yield _emit(role, piece)
        else:
            calls = part.get("tool_calls") or []
    for role, piece in splitter.spill():
        yield _emit(role, piece)

    content = "".join(raw_parts)
    assistant = {"role": "assistant", "content": content}
    if reasoning:
        assistant["reasoning_content"] = "".join(reasoning)

    if calls:
        assistant["tool_calls"] = [
            {"id": c.get("id", ""), "type": "function", "function": {
                "name": (c.get("function") or {}).get("name", ""),
                "arguments": (c.get("function") or {}).get("arguments", "")}}
            for c in calls
        ]
    tool_msgs = []
    for call in calls:
        name, args, call_id = normalize_native_toolcall(call)
        result = None
        async for item in _guarded_run(guard, security, functions, name, args):
            if item[0] == "event":
                yield ("event", item[1])
            else:
                result = item[1]
        tool_msgs.append({"role": "tool", "content": result,
                          "tool_call_id": call_id})
        yield ("event", {"type": "toolcall", "tool": name, "args": args,
                         "result": result})
    yield ("turn", assistant, tool_msgs)


async def stream_agent(pid: str, message: str, user: str):
    """One agent run over a project, streamed as SSE events.
    Ends on a "done" event, or a single "error" one like the other streams —
    nothing raises out of the router. Tool calls travel natively (the server
    gets the tools array) when it supports them, pythonically between
    sentinels when it does not; a call that fails to parse or names no known
    tool comes back as a toolcall carrying the refusal, so the loop answers
    from it instead of dying.

    The run continues the caller's per-session conversation
    (content.sessions): it starts from the history the caller's earlier runs
    left, and on success folds its own exchange back in. A failed run writes
    nothing, and a second run while one is streaming is refused
    """
    sess = sessions.get(pid, user)
    if sess.lock.locked():
        yield {"type": "error",
               "error": "The agent is already answering — wait for it to finish."}
        return
    await sess.lock.acquire()
    try:
        project = await projects.get_project(pid)
        if project is None:
            raise ValueError("project not found")

        mode = await llm.resolve_tool_mode()
        tools, functions, security = await agent.select_tools(
            pid, user, native=(mode == "native"))
        guard = tool_guard.ToolGuard(pid, user)

        system = llm.load_prompt(
            "project_agent",
            project_name=project['name'],
            project_description=project['description'],
            tools=json.dumps(tools) if isinstance(tools, (list, tuple)) else tools,
            tool_style=(
                "You may call the tools below by emitting tool calls — the "
                "server formats them for you, so never write a call's syntax "
                "into your answer. Do not describe a call you are making; "
                "just make it."
                if mode == "native"
                else "You should use pythonic-style toolcalls, not json or any "
                     "other format. For example a `fn` tool taking 2 arguments "
                     "should be called as: `fn(arg1=value1, arg2=value2)`"
            ),
        )

        yield {"type": "token", "role": "user", "text": message}
        streamed = [{"type": "token", "role": "user", "text": message}]

        # the run continues the session's history: the system prompt is
        # rebuilt fresh, everything after it picks up where the last run
        # stopped
        chat = [
            {'role': 'system', 'content': system},
            *sess.chat,
            {'role': 'user', 'content': message}
        ]
        while True:
            turn = (_native_turn(chat, tools, functions, guard, security)
                    if mode == "native"
                    else _text_turn(chat, functions, guard, security))
            assistant, tool_msgs = None, []
            async for item in turn:
                if item[0] == "turn":
                    _, assistant, tool_msgs = item
                else:
                    ev = item[1]
                    yield ev
                    if ev["type"] in ("token", "toolcall"):
                        streamed.append(ev)
            chat.append(assistant)
            chat.extend(tool_msgs)
            if not tool_msgs:
                break

        # a finished run only: its messages carry on, its events redraw the
        # thread after a reload
        sessions.remember(sess, chat[1:], streamed)
        yield {"type": "done", "chars": 0}
    except Exception as e:
        yield {"type": "error", "error": str(e)}
    finally:
        sess.lock.release()
