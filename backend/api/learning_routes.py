from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from backend.api.deps import get_source_or_404, sse, sse_headers
from backend.content import services
from backend.data import store


router = APIRouter()


@router.post("/api/quiz/{sid}/generate")
async def quiz_generate(sid: str, body: Optional[dict] = None):
    await get_source_or_404(sid)
    body = body or {}
    events = services.stream_quiz(
        sid,
        scope=body.get("scope", "both"),
        num_questions=int(body.get("num_questions", 5)),
        difficulty=body.get("difficulty", "medium"),
        language=body.get("language", "English"),
    )
    return StreamingResponse(sse(events), media_type="text/event-stream", headers=sse_headers())

@router.get("/api/quiz/{sid}")
async def quiz_get(sid: str):
    await get_source_or_404(sid)
    return await store.read_quiz(sid)

@router.get("/api/quiz/{sid}/order")
async def quiz_order(sid: str):
    await get_source_or_404(sid)
    return {"ok": True, "questions": await services.quiz_order(sid)}

@router.get("/api/quiz/{sid}/stats")
async def quiz_stats(sid: str):
    await get_source_or_404(sid)
    return {"ok": True, "stats": await store.read_stats(sid)}

@router.post("/api/quiz/{sid}/answer")
async def quiz_record(sid: str, body: Optional[dict] = None):
    await get_source_or_404(sid)
    body = body or {}
    qid = body.get("question_id")
    if not qid:
        raise HTTPException(400, "missing question_id")
    q = await services.record_answer(sid, qid, bool(body.get("success")))
    return {"ok": True, "question": q}

@router.post("/api/summarize/{sid}")
async def summarize(sid: str, body: Optional[dict] = None):
    await get_source_or_404(sid)
    body = body or {}
    events = services.stream_summarize(
        sid,
        scope=body.get("scope", "both"),
        length=body.get("length", "medium"),
        language=body.get("language", "English"),
    )
    return StreamingResponse(sse(events), media_type="text/event-stream", headers=sse_headers())
