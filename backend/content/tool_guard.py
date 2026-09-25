"""The agent's tool-call guard: security levels, the policy that decides
what a call of each level may do, and the parking spot of a pending
approval.

Every tool declares a security level (``read``, ``write``, …) where it is
defined in ``content.tools``. The policy — one of ``allow`` / ``prompt`` /
``deny`` per level, plus per-tool overrides — comes from the config,
resolved once at import (``core.config.AGENT_TOOL_SECURITY``).

``allow`` runs the tool as before. ``deny`` refuses it with a string the
model reads, the way an unknown tool or a crash is already refused. ``prompt``
pauses the run until the caller answers an approval prompt from the browser:
the guard parks an ``asyncio.Future`` here, the run's generator yields an
``approval_request`` SSE event and awaits the future, and the answer arrives
on a separate ``POST /api/agent/approval/{pid}`` (``agent_routes``) that
resolves it. No LLM connection is held across the wait — tool calls run
between the model's turns, never during one — and a wait past the configured
timeout is refused like a denial, so a forgotten prompt cannot pin a session
forever.

There can be at most one pending approval per (project, user), because the
run lock already serializes a pair's runs.
"""

import asyncio
import secrets

from ..core import config

VERDICTS = ("allow", "prompt", "deny")


class _Pending:
    """One parked approval: what it asks about, and the future the answer
    resolves
    """
    __slots__ = ("request_id", "tool", "args", "future")

    def __init__(self, request_id: str, tool: str, args: dict, future):
        self.request_id = request_id
        self.tool = tool
        self.args = args
        self.future = future


_pending: dict = {}   # (pid, user) -> _Pending


class ToolGuard:
    """The gate between one agent run and the tools it calls. One per run,
    built with the (pid, user) pair the approval prompts are keyed by
    """

    def __init__(self, pid: str, user: str,
                 policy: dict | None = None, timeout: float | None = None):
        self.pid = pid
        self.user = user
        self.policy = policy if policy is not None else config.AGENT_TOOL_SECURITY
        self.timeout = (timeout if timeout is not None
                        else config.AGENT_APPROVAL_TIMEOUT)

    def policy_for(self, tool: str, security: str) -> str:
        """The verdict a call of ``tool`` at level ``security`` gets: its
        override if one names it, else the level's policy. A tool declaring a
        level the policy does not know, or none at all, is denied — the
        strictest reading of an unstated risk
        """
        override = self.policy["overrides"].get(tool)
        if override:
            return override
        return self.policy["levels"].get(security, "deny")

    def begin(self, name: str, security: str, args: dict) -> tuple:
        """The first step of ``check``: (outcome, payload) where outcome is
        ``allow`` (payload None — just run the tool), ``deny`` (payload the
        refusal string), or ``prompt`` (payload the request id to wait on)
        """
        verdict = self.policy_for(name, security)
        if verdict == "allow":
            return ("allow", None)
        if verdict == "deny":
            return ("deny", f"tool call refused: {name} is not allowed by policy")
        request_id = secrets.token_urlsafe(8)
        loop = asyncio.get_running_loop()
        _pending[(self.pid, self.user)] = _Pending(
            request_id, name, args, loop.create_future())
        return ("prompt", request_id)

    async def wait(self, request_id: str) -> str:
        """The parked future's answer: ``allow``, ``deny``, or ``timeout``
        when nobody answered in time. The parking spot is cleared either way
        """
        entry = _pending.get((self.pid, self.user))
        try:
            return await asyncio.wait_for(entry.future, timeout=self.timeout)
        except asyncio.TimeoutError:
            return "timeout"
        finally:
            _pending.pop((self.pid, self.user), None)


def resolve(pid: str, user: str, request_id: str, decision: str) -> bool:
    """The approval route's answer to a pending prompt: True when a matching
    one was waiting and is now resolved, False when there is none (a stale or
    doubled click, the run already over). Only the pair's own prompt can be
    resolved — the registry is keyed by (pid, user)
    """
    entry = _pending.get((pid, user))
    if entry is None or entry.request_id != request_id or entry.future.done():
        return False
    entry.future.set_result(decision)
    return True


def cancel(pid: str, user: str) -> None:
    """Unblocks a pending approval as if denied — the New-conversation path,
    so a forgotten prompt does not hold a run that is about to be dropped
    """
    entry = _pending.pop((pid, user), None)
    if entry and not entry.future.done():
        entry.future.set_result("deny")
