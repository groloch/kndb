"""Bugs that were once live, pinned so they stay fixed.
"""

import asyncio

import httpx
import pytest

from backend.core import db
from backend.core.db import Source
from backend.data import store
from backend.integrations import llm


def _run(body):
    """Runs one coroutine factory against a fresh engine.
    dispose on both ends, so the engine is never carried across event loops
    """

    async def main():
        await db.dispose()
        await db.init_db()
        try:
            return await body()
        finally:
            await db.dispose()

    return asyncio.run(main())


def test_find_by_url_survives_duplicate_urls():
    """ix_sources_url is not unique, so scalar_one_or_none used to raise
    MultipleResultsFound instead of answering the dedup probe
    """
    url = "https://example.invalid/duplicated"

    async def body():
        async with db.session() as s:
            s.add(Source(id="src_dup_second", title="second", source_type="md",
                         source_path="sources/src_dup_second.md", url=url,
                         fetched_at="2026-01-02T00:00:00"))
            s.add(Source(id="src_dup_first", title="first", source_type="md",
                         source_path="sources/src_dup_first.md", url=url,
                         fetched_at="2026-01-01T00:00:00"))
            await s.commit()
        return await store.find_by_url(url)

    found = _run(body)
    assert found is not None
    assert found["id"] == "src_dup_first"   # earliest import wins, deterministically


def test_find_by_url_is_none_when_nothing_matches():
    async def body():
        return (await store.find_by_url(""),
                await store.find_by_url("https://example.invalid/never-fetched"))

    assert _run(body) == (None, None)


def test_chat_records_why_it_failed(monkeypatch):
    """chat assigned _last_error without `global`, so the assignment was
    function-local and last_error() never saw it
    """

    class _Dead:
        async def post(self, *a, **kw):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(llm, "get_client", lambda: _Dead())
    monkeypatch.setattr(llm, "_backoff", lambda attempt: asyncio.sleep(0))
    monkeypatch.setattr(llm, "_last_error", None, raising=False)

    with pytest.raises(llm.LLMError):
        asyncio.run(llm.chat("system", "user"))

    assert llm.last_error() == "connection refused"


def test_stream_chat_records_why_it_failed(monkeypatch):
    """Same bug in the streaming path
    """

    class _Dead:
        def stream(self, *a, **kw):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(llm, "get_client", lambda: _Dead())
    monkeypatch.setattr(llm, "_backoff", lambda attempt: asyncio.sleep(0))
    monkeypatch.setattr(llm, "_last_error", None, raising=False)

    async def drain():
        async for _ in llm.stream_chat("system", "user"):
            pass

    with pytest.raises(llm.LLMError):
        asyncio.run(drain())

    assert llm.last_error() == "connection refused"
