from typing import Optional

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from backend.api.deps import (sse, sse_headers)
from backend.content import services


router = APIRouter()


@router.post("/api/agent/message/{pid}")
async def get_agent_message(pid: str, body: dict):
        
    return StreamingResponse(
        sse(services.stream_agent(pid, body.get('message'))),
        media_type="text/event-stream",
        headers=sse_headers()
    )
