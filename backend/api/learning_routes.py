"""Quizzes and summaries, the two things the model is asked to write.
A quiz is private: it always lives in the caller's own workspace, so scores and
questions never leak between members of a shared project
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from backend.api.deps import (WRITE, current_user, get_note_or_404,
                              get_source_or_404, require_role, sse,
                              sse_headers)
from backend.content import services
from backend.data import projects
from backend.data import quiz as quiz_store


router = APIRouter()


async def personal_pid(user: str = Depends(current_user)) -> str:
    """The caller's own workspace, which is where every quiz below is kept
    """
    return await projects.personal_project_id(user)

@router.post("/api/quiz/{sid}/generate")
async def quiz_generate(sid: str, body: Optional[dict] = None,
                        pid: str = Depends(personal_pid)):
    """Writes a quiz from the caller's notes, the document, or both, and
    streams the model's tokens as it goes.
    Replaces whatever quiz the caller had on this source, and reports a
    failure as a final error event rather than an HTTP status
    """
    await get_source_or_404(sid)
    body = body or {}
    events = services.stream_quiz(
        pid, sid,
        scope=body.get("scope", "both"),
        num_questions=int(body.get("num_questions", 5)),
        difficulty=body.get("difficulty", "medium"),
        language=body.get("language", "English"),
    )
    return StreamingResponse(sse(events), media_type="text/event-stream", headers=sse_headers())

@router.get("/api/quiz/{sid}")
async def quiz_get(sid: str, pid: str = Depends(personal_pid)):
    """The caller's quiz on this source, an empty one when none was ever made
    """
    await get_source_or_404(sid)
    return await quiz_store.read_quiz(pid, sid)

@router.get("/api/quiz/{sid}/order")
async def quiz_order(sid: str, pid: str = Depends(personal_pid)):
    """The questions in the order to play them.
    Whatever is due comes first, and within that the worst answered
    """
    await get_source_or_404(sid)
    return {"ok": True, "questions": await services.quiz_order(pid, sid)}

@router.get("/api/quiz/{sid}/stats")
async def quiz_stats(sid: str, pid: str = Depends(personal_pid)):
    """The caller's record on this source: counts, box and next review per
    question, empty when nothing was ever played
    """
    await get_source_or_404(sid)
    return {"ok": True, "stats": await quiz_store.read_stats(pid, sid)}

@router.post("/api/quiz/{sid}/answer")
async def quiz_record(sid: str, body: Optional[dict] = None,
                      pid: str = Depends(personal_pid)):
    """The question's record after the answer.
    Getting it right moves it a box up and pushes the next review further out,
    getting it wrong drops it to the bottom box, due again straight away
    """
    await get_source_or_404(sid)
    body = body or {}
    qid = body.get("question_id")
    if not qid:
        raise HTTPException(400, "missing question_id")
    q = await services.record_answer(pid, sid, qid, bool(body.get("success")))
    return {"ok": True, "question": q}

@router.post("/api/quiz/{sid}/questions")
async def quiz_question_add(sid: str, body: Optional[dict] = None,
                            pid: str = Depends(personal_pid)):
    """A hand-written question, appended to the caller's quiz.
    400 when the wording is empty, the answers too few or the right one out of
    range
    """
    await get_source_or_404(sid)
    body = body or {}
    try:
        q = await services.add_quiz_question(
            pid, sid,
            question=body.get("question", ""),
            answers=body.get("answers") or [],
            answer_index=body.get("answer_index", 0),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "question": q}

@router.put("/api/quiz/{sid}/questions/{qid}")
async def quiz_question_update(sid: str, qid: str, body: Optional[dict] = None,
                               pid: str = Depends(personal_pid)):
    """The rewritten question, whichever of its fields the body carries.
    Its record is left alone, so an edited question keeps the box it earned
    """
    await get_source_or_404(sid)
    body = body or {}
    try:
        q = await services.update_quiz_question(
            pid, sid, qid,
            question=body.get("question"),
            answers=body.get("answers"),
            answer_index=body.get("answer_index"),
        )
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "question": q}

@router.delete("/api/quiz/{sid}/questions/{qid}")
async def quiz_question_delete(sid: str, qid: str,
                               pid: str = Depends(personal_pid)):
    """Drops a question and the record it had, 404 when the quiz has no such id
    """
    await get_source_or_404(sid)
    try:
        await services.delete_quiz_question(pid, sid, qid)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}

@router.post("/api/summarize/{sid}")
async def summarize(sid: str, body: Optional[dict] = None,
                    user: str = Depends(current_user)):
    """Streams a summary of the document and appends it to a note page.
    Unlike the quiz routes this one writes where the note lives, so it needs a
    writing role in that project, and the page must be one of this very source
    """
    await get_source_or_404(sid)
    body = body or {}
    nid = (body.get("note_id") or "").strip()
    if not nid:
        raise HTTPException(400, "missing note_id")
    page = await get_note_or_404(nid)
    await require_role(page["project_id"], user, *WRITE)
    if page["source_id"] != sid:
        raise HTTPException(400, "that note does not belong to this source")

    events = services.stream_summarize(
        page["project_id"], sid, nid, user,
        length=body.get("length", "medium"),
        language=body.get("language", "English"),
    )
    return StreamingResponse(sse(events), media_type="text/event-stream", headers=sse_headers())
