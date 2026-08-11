import asyncio
import json
import os
import re

import httpx

from ..core import config

BASE_DIR = config.BASE_DIR
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")

BASE_URL = config.LLM_BASE_URL
API_KEY = config.LLM_API_KEY
MODEL = config.LLM_MODEL
MAX_RETRIES = config.LLM_RETRIES

_RETRYABLE = {429, 500, 502, 503, 504}

_client: httpx.AsyncClient | None = None
_probe_lock = asyncio.Lock()
_ready: bool | None = None
_resolved_model = MODEL
_last_error: str | None = None


class LLMError(RuntimeError):
    """LLM-side failure with user-actionable message."""


def _make_client() -> httpx.AsyncClient:
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    return httpx.AsyncClient(
        base_url=BASE_URL,
        headers=headers,
        timeout=httpx.Timeout(connect=10.0, read=None, write=60.0, pool=10.0),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
    )

def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = _make_client()
    return _client

async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None

def _llm_error(action: str, detail) -> LLMError:
    return LLMError(
        f"{action} — {detail}\n"
        "Set llm: base_url / api_key / model in kndb.yaml to point at an "
        "OpenAI-compatible server (vLLM, llama.cpp server, Ollama, ...)."
    )

async def probe() -> bool:
    """Reach the server and pick a usable model id."""
    global _ready, _resolved_model, _last_error
    client = get_client()
    try:
        r = await client.get("models", timeout=(10.0, 30.0))
    except Exception as e:  # noqa: BLE001
        _ready = False
        _last_error = str(e)
        return False
    if r.status_code >= 300:
        _ready = False
        _last_error = f"LLM server returned HTTP {r.status_code}"
        return False
    _ready = True
    _last_error = None
    _resolved_model = MODEL
    try:
        ids = [m.get("id", "") for m in (r.json().get("data") or []) if isinstance(m, dict)]
    except Exception:
        ids = []
    if ids and MODEL not in ids and len(ids) == 1:
        _resolved_model = ids[0]
    return True

async def is_loaded() -> bool:
    global _ready
    if _ready is None:
        async with _probe_lock:
            if _ready is None:
                await probe()
    elif _ready is False:
        async with _probe_lock:
            await probe()
    return _ready is True

def last_error():
    return _last_error

def model_display() -> str:
    return f"{_resolved_model} @ {BASE_URL}"

def load_prompt(name: str, **kwargs) -> str:
    """Read prompts/<name>.md and substitute {{placeholder}} values."""
    path = os.path.join(PROMPTS_DIR, name + ".md")
    with open(path, encoding="utf-8") as f:
        text = f.read()

    def sub(m):
        return str(kwargs.get(m.group(1), m.group(0)))

    return re.sub(r"\{\{(\w+)\}\}", sub, text)

def _messages(system: str, user: str) -> list:
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

async def _backoff(attempt: int) -> None:
    await asyncio.sleep(min(0.4 * (2 ** attempt), 8.0))

async def chat(system: str, user: str, max_tokens: int | None = None,
               temperature: float = 0.6) -> str:
    payload = {
        "model": _resolved_model,
        "messages": _messages(system, user),
        "temperature": temperature,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    client = get_client()
    last: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            r = await client.post("chat/completions", json=payload)
        except httpx.TransportError as e:
            last = e
            _last_error = str(e)
            if attempt + 1 < MAX_RETRIES:
                await _backoff(attempt)
                continue
            raise _llm_error("Could not reach the LLM server", e)
        if r.status_code >= 300:
            if r.status_code in _RETRYABLE and attempt + 1 < MAX_RETRIES:
                await _backoff(attempt)
                continue
            detail = r.text[:300] or f"HTTP {r.status_code}"
            raise _llm_error(f"LLM server returned HTTP {r.status_code}", detail)
        try:
            content = r.json()["choices"][0]["message"]["content"]
        except Exception as e:  # noqa: BLE001
            raise _llm_error("Malformed response from the LLM server", e)
        _last_error = None
        return str(content or "").strip()
    raise _llm_error("LLM request failed after retries", last or "unknown error")

async def stream_chat(system: str, user: str, max_tokens: int | None = None,
                      temperature: float = 0.6):
    payload = {
        "model": _resolved_model,
        "messages": _messages(system, user),
        "temperature": temperature,
        "stream": True,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    client = get_client()
    attempt = 0
    while True:
        attempt += 1
        started = False
        try:
            async with client.stream("POST", "chat/completions", json=payload) as resp:
                if resp.status_code >= 300:
                    body = (await resp.aread())[:300]
                    if resp.status_code in _RETRYABLE and attempt < MAX_RETRIES:
                        await _backoff(attempt - 1)
                        continue
                    detail = body.decode("utf-8", "replace") or f"HTTP {resp.status_code}"
                    raise _llm_error(f"LLM server returned HTTP {resp.status_code}", detail)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    chunk = line[len("data:"):].strip()
                    if not chunk or chunk == "[DONE]":
                        continue
                    try:
                        obj = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    msg = choices[0].get("delta") or choices[0].get("message") or {}
                    token = msg.get("content")
                    if token:
                        started = True
                        yield token
                _last_error = None
                return
        except httpx.TransportError as e:
            _last_error = str(e)
            if started or attempt >= MAX_RETRIES:
                raise _llm_error("LLM stream failed mid-request", e)
            await _backoff(attempt - 1)
        except LLMError:
            raise

def extract_json(text: str):
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text)
    starts = [i for i in (text.find("["), text.find("{")) if i != -1]
    if not starts:
        raise ValueError("Model output contained no JSON object/array")
    start = min(starts)
    for candidate in (text[start:], _scan_json(text, start)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise ValueError("Model output was not valid JSON")

def _scan_json(text: str, start: int) -> str:
    stack = []
    pairs = {"[": "]", "{": "}"}
    closing = {"]": "[", "}": "{"}
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c in pairs:
            stack.append(c)
        elif c in closing:
            if not stack or stack[-1] != closing[c]:
                return text[start:i]
            stack.pop()
            if not stack:
                return text[start: i + 1]
    return text[start:]

async def html_to_markdown(content: str) -> str:
    system = load_prompt("web_to_markdown")
    user = "Raw website content:\n=====\n" + str(content)[:14000] + "\n====="
    return await chat(system, user, max_tokens=2500, temperature=0.3)
