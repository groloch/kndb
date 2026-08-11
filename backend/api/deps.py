import json

from fastapi import HTTPException

from backend.data import projects, store


async def get_source_or_404(sid: str) -> dict:
    row = await store.get_source(sid)
    if not row:
        raise HTTPException(404, f"source {sid} not found")
    return row

async def get_project_or_404(pid: str) -> dict:
    p = await projects.get_project(pid)
    if not p:
        raise HTTPException(404, f"project {pid} not found")
    return p

def sse(events):
    async def gen():
        async for ev in events:
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\\n\\n"

    return gen()

def sse_headers() -> dict:
    return {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }

async def project_sources_rows(pid: str) -> list:
    srcs = []
    for sid in (await projects.get_project(pid))["source_ids"]:
        row = await store.get_source(sid)
        if row:
            srcs.append(store.to_public(row))
    return srcs
