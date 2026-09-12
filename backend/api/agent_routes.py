"""The project agent's single route: one stateless run, streamed as SSE.

Membership is checked before anything streams. The loop itself — tool
preparation, the reasoning/tool-call token split, the feedback of tool results
— lives in ``services.stream_agent``, and the tools in ``content.agent``, so a
feature the agent should gain belongs there, not here
"""

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from backend.api.deps import current_user, require_role, sse, sse_headers
from backend.content import services


router = APIRouter()


@router.post("/api/agent/message/{pid}")
async def get_agent_message(pid: str, body: dict,
                            user: str = Depends(current_user)):
    """One agent run for a member of the project, streamed as SSE events:
    token (roles user/assistant/assistant-trace), toolcall, done or error.
    The tools are read-only, so membership alone suffices
    """
    await require_role(pid, user)
    return StreamingResponse(
        sse(services.stream_agent(pid, body.get('message'))),
        media_type="text/event-stream",
        headers=sse_headers()
    )