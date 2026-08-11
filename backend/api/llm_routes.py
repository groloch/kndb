from fastapi import APIRouter

from backend.integrations import llm


router = APIRouter()


@router.get("/api/llm/status")
async def llm_status():
    return {
        "ok": True,
        "loaded": await llm.is_loaded(),
        "model": llm.model_display(),
        "last_error": llm.last_error(),
    }
