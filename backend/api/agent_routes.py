"""The project agent's routes: one streamed run, the tool-approval side
channel, plus the session's replay and reset.

Membership is checked before anything happens. The loop itself — tool
preparation, the reasoning/tool-call token split, the guarding of tool calls
and the feedback of tool results — lives in ``services.stream_agent``, the
tools in ``content.agent``, declared in the ``content.tools`` package, the
guard in ``content.tool_guard``, and the
per-session memory in ``content.sessions``, so a feature the agent should
gain belongs there, not here
"""

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from backend.api.deps import current_user, require_role, sse, sse_headers
from backend.content import sessions
from backend.content import services
from backend.content import tool_guard


router = APIRouter()


@router.post("/api/agent/message/{pid}")
async def get_agent_message(pid: str, body: dict,
                            user: str = Depends(current_user)):
    """One agent run for a member of the project, streamed as SSE events:
    token (roles user/assistant/assistant-trace), toolcall, done or error.
    The run continues the caller's per-session conversation; a second run
    while one is streaming is refused with an error event
    """
    await require_role(pid, user)
    return StreamingResponse(
        sse(services.stream_agent(pid, body.get('message'), user)),
        media_type="text/event-stream",
        headers=sse_headers()
    )


@router.post("/api/agent/approval/{pid}")
async def post_approval(pid: str, body: dict,
                        user: str = Depends(current_user)):
    """The caller's answer to a pending tool-approval prompt of their own
    run: {request_id, decision: "allow" | "deny"}. A side channel that
    deliberately does not touch the run lock — the lock is held by the run
    that is waiting. No matching pending prompt (a stale or doubled click,
    the run already over) is a refused {ok:false}
    """
    await require_role(pid, user)
    request_id = body.get("request_id") or ""
    decision = body.get("decision")
    if not isinstance(request_id, str) or not request_id \
            or decision not in ("allow", "deny"):
        return {"ok": False, "error": "request_id and decision (allow|deny) required"}
    if not tool_guard.resolve(pid, user, request_id, decision):
        return {"ok": False, "error": "no pending approval"}
    return {"ok": True}


@router.get("/api/agent/session/{pid}")
async def get_agent_session(pid: str, user: str = Depends(current_user)):
    """The caller's session display log — the token and toolcall events of
    the finished runs — so a reloaded page redraws the thread as it was
    """
    await require_role(pid, user)
    return {"events": sessions.replay(pid, user)}


@router.delete("/api/agent/session/{pid}")
async def delete_agent_session(pid: str, user: str = Depends(current_user)):
    """Forgets the caller's conversation: the next run starts fresh
    """
    await require_role(pid, user)
    tool_guard.cancel(pid, user)
    sessions.clear(pid, user)
    return {"ok": True}
