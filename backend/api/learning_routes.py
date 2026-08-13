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
    return await projects.personal_project_id(user)

@router.post("/api/quiz/{sid}/generate")
async def quiz_generate(sid: str, body: Optional[dict] = None,
                        pid: str = Depends(personal_pid)):
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
    await get_source_or_404(sid)
    return await quiz_store.read_quiz(pid, sid)

@router.get("/api/quiz/{sid}/order")
async def quiz_order(sid: str, pid: str = Depends(personal_pid)):
    await get_source_or_404(sid)
    return {"ok": True, "questions": await services.quiz_order(pid, sid)}

@router.get("/api/quiz/{sid}/stats")
async def quiz_stats(sid: str, pid: str = Depends(personal_pid)):
    await get_source_or_404(sid)
    return {"ok": True, "stats": await quiz_store.read_stats(pid, sid)}

@router.post("/api/quiz/{sid}/answer")
async def quiz_record(sid: str, body: Optional[dict] = None,
                      pid: str = Depends(personal_pid)):
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
    await get_source_or_404(sid)
    try:
        await services.delete_quiz_question(pid, sid, qid)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}

@router.post("/api/summarize/{sid}")
async def summarize(sid: str, body: Optional[dict] = None,
                    user: str = Depends(current_user)):
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
        scope=body.get("scope", "both"),
        length=body.get("length", "medium"),
        language=body.get("language", "English"),
    )
    return StreamingResponse(sse(events), media_type="text/event-stream", headers=sse_headers())
