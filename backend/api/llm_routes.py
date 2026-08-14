from fastapi import APIRouter

from backend.integrations import llm


router = APIRouter()


@router.get("/api/llm/status")
async def llm_status():
    """Whether a model is reachable, its display name, the last error.
    Re-probes the server whenever the state is unknown or was a failure, so
    the call can block on the network
    """
    return {
        "ok": True,
        "loaded": await llm.is_loaded(),
        "model": llm.model_display(),
        "last_error": llm.last_error(),
    }
