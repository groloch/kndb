import asyncio
import time
from datetime import datetime, timedelta, timezone

from ..data import store
from ..integrations import llm


_INTERVALS = [0, 1, 2, 4, 7, 15, 30, 60]
_LENS = {"short": "~150 words", "medium": "~400 words", "detailed": "~900 words"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _local_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")

async def material_for(scope: str, row: dict):
    """Return ``(notes_text, document_text)`` depending on the requested scope."""
    notes, doc = "", ""
    if scope in ("notes", "both"):
        notes = row.get("notes") or ""
    if scope in ("document", "both"):
        doc = await document_text(row)
    return notes, doc

async def document_text(row: dict) -> str:
    stype, path = row["source_type"], row["source_path"]

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

    if stype == "md":
        return await asyncio.to_thread(_md)
    if stype in ("html", "html+css"):
        return await asyncio.to_thread(_html)
    if stype == "pdf":
        return await asyncio.to_thread(_pdf)
    return ""

def _check_material(scope: str, notes: str, doc: str) -> None:
    if scope == "notes" and not notes.strip():
        raise ValueError("There are no personal notes for this source yet — "
                         "write some notes first, or pick a different scope.")
    if scope == "document" and not doc.strip():
        raise ValueError("Could not extract text from the document — pick "
                         "'Notes only', or import a document with text.")
    if scope == "both" and not notes.strip() and not doc.strip():
        raise ValueError("Both the notes and the document are empty for this source.")

def _normalize_questions(data):
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
    stats["num_questions"] = len(questions)
    stats["date_last_modified"] = _local_now()
    base = {"times_played": 0, "times_successful": 0, "box": 0,
            "next_review": "", "last_played": ""}
    for q in questions:
        stats["questions"].setdefault(q["id"], dict(base))

async def stream_quiz(sid: str, scope: str = "both", num_questions: int = 5,
                      difficulty: str = "medium", language: str = "English"):
    try:
        row = await store.get_source(sid)
        if not row:
            raise ValueError("source not found")
        num_questions = max(1, min(int(num_questions), 20))
        notes, doc = await material_for(scope, row)
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
        async for token in llm.stream_chat(
            system, "Generate the quiz based on the material above.",
            max_tokens=2048, temperature=0.5,
        ):
            text += token
            yield {"type": "token", "text": token}

        questions = _normalize_questions(llm.extract_json(text))
        await store.save_quiz(sid, {
            "source_id": sid, "updated_at": _local_now(), "questions": questions,
        })
        stats = await store.read_stats(sid)
        if stats:
            _init_question_stats(stats, questions)
            await store.save_stats(sid, stats)
        yield {"type": "done", "questions": len(questions)}
    except Exception as e:
        yield {"type": "error", "error": str(e)}

async def record_answer(sid: str, qid: str, success: bool) -> dict:
    stats = await store.read_stats(sid)
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
    await store.save_stats(sid, stats)
    return q

async def quiz_order(sid: str) -> list:
    quiz = await store.read_quiz(sid)
    stats = await store.read_stats(sid)
    now = _now()

    def key(q):
        st = stats.get("questions", {}).get(q["id"], {})
        nr = st.get("next_review") or ""
        due = (not nr) or nr <= now
        played = st.get("times_played", 0)
        rate = st.get("times_successful", 0) / max(played, 1)
        return (0 if due else 1, rate, st.get("box", 0), q.get("id", ""))

    return sorted(quiz.get("questions", []), key=key)

async def stream_summarize(sid: str, scope: str = "both", length: str = "medium",
                           language: str = "English"):
    """Summarize, streaming token events; appends the summary to the notes."""
    try:
        row = await store.get_source(sid)
        if not row:
            raise ValueError("source not found")
        notes, doc = await material_for(scope, row)
        _check_material(scope, notes, doc)

        system = llm.load_prompt(
            "summarization",
            instructions=f"Length: {_LENS.get(length, length)}. Write in {language}.",
        )
        user = (
            "Material - personal notes:\n"
            "=====\n"
            f"{notes[:6000] or '(no notes provided)'}\n"
            "=====\n\n"
            "Material - source document:\n"
            "=====\n"
            f"{doc[:10000] or '(no document text provided)'}\n"
            "=====\n\n"
            "Write the summary now."
        )
        text = ""
        async for token in llm.stream_chat(
            system, user, temperature=0.4,
        ):
            text += token
            yield {"type": "token", "text": token}

        summary = text.strip()
        current_notes = row.get("notes") or ""
        await store.save_note(sid, current_notes.rstrip()
                              + f"\n\n---\n\n## Summary — {_local_now()}\n\n{summary}\n")
        yield {"type": "done", "chars": len(summary)}
    except Exception as e:
        yield {"type": "error", "error": str(e)}
