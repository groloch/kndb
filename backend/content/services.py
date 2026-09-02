"""What the app does with a source once it is stored: quizzes, summaries,
spaced repetition.
Reads the note pages and the document text, streams the model's answer, and
writes the result back through the quiz and note stores
"""

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone

from ..data import notes as note_store
from ..data import quiz as quiz_store
from ..data import store, projects
from ..integrations import llm
from ..content.agent import prepare_tools, parse_json_toolcall, parse_pythonic_toolcall


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

async def stream_agent(pid: str, message: str):
    project = await projects.get_project(pid)

    tools, functions = prepare_tools(pid)

    system = llm.load_prompt(
        "project_agent",
        project_name=project['name'],
        project_description=project['description'],
        tools=tools,
    )

    yield {"type": "token", "role": "user", "text": message}

    chat = [
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': message}
    ]
    while True:
        in_reasoning = False
        in_tool = False
        current_tool_call = ''
        current_assistant_trace = ''

        async for token in llm.stream_chat(
            chat, temperature=0.8
        ):
            current_assistant_trace += token
            if token == '<think>':
                in_reasoning = True
                continue
            elif token == '</think>':
                in_reasoning = False
                continue

            if token == '<|tool_call_start|>':
                in_tool = True
                continue
            elif token == '<|tool_call_end|>':
                in_tool = False
                continue

            if in_tool:
                current_tool_call += token
            else:
                role = "assistant" if not in_reasoning else "assistant-trace"
                yield {"type": "token", "role": role, "text": token}
        chat.append({
            'role': 'assistant',
            'content': current_assistant_trace
        })
        if len(current_tool_call.strip()) == 0:
            break

        tool, tool_args = parse_pythonic_toolcall(current_tool_call)
        if tool not in functions:
            tool_result = f'unknown tool: {tool}'
        else:
            tool_result = await functions[tool](**tool_args)
        chat.append({
            'role': 'tool',
            'content': tool_result
        })

        yield {"type": "toolcall", "tool": tool, "args": tool_args,
               "result": tool_result}

    yield {"type": "done", "chars": 0}
