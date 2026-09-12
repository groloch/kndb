"""The OpenAI-compatible chat server the app talks to.
One shared client, the retry policy, and the prompt loader — everything that
knows about the model endpoint lives here.
Failures come back as LLMError, whose message is meant to be shown as-is
"""

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

# The agent's tool-call transport, resolved once per model by a short probe:
# "native" (the server gets the tools array) or "text" (pythonic sentinel
# calls written into the answer).
_tool_mode: str | None = None
_tool_mode_for: str = ""
_tool_mode_lock = asyncio.Lock()


class LLMError(RuntimeError):
    """LLM-side failure, carrying a message the user can act on
    """


def _make_client() -> httpx.AsyncClient:
    """Client for the configured server.
    No read timeout: a long generation is not a hung request, so only connect,
    write and pool are bounded
    """
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
    """Drops the shared client, so the next call builds a fresh one
    """
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None

def _llm_error(action: str, detail) -> LLMError:
    """LLMError spelling out what failed and which kndb.yaml keys would fix it
    """
    return LLMError(
        f"{action} — {detail}\n"
        "Set llm: base_url / api_key / model in kndb.yaml to point at an "
        "OpenAI-compatible server (vLLM, llama.cpp server, Ollama, ...)."
    )

async def probe() -> bool:
    """Reaches the server and settles on a model id, False when it is down.
    Never raises — the reason is kept in last_error for the status endpoint.
    A server offering exactly one model overrides the configured id, so a
    local server with one model loaded needs no configuring
    """
    global _ready, _resolved_model, _last_error
    client = get_client()
    try:
        r = await client.get("models", timeout=(10.0, 30.0))
    except Exception as e:
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
    """Whether the server is reachable, probing at most once per caller.
    A failed probe is retried on the next call, a successful one is never
    repeated
    """
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
    """Reads prompts/<name>.md and substitutes {{placeholder}} values.
    Placeholders with no matching keyword are left as they stand
    """
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
    """Waits before a retry, doubling per attempt up to 8s
    """
    await asyncio.sleep(min(0.4 * (2 ** attempt), 8.0))

async def chat(system: str, user: str, max_tokens: int | None = None,
               temperature: float = 0.6) -> str:
    """One completion, retried on transport errors and on 429/5xx.
    Raises LLMError on an unreachable server, on a status that is not
    retryable or has run out of retries, and on a reply that does not parse.
    Nothing bounds the wait for tokens, so a slow generation blocks instead of
    failing
    """
    global _last_error
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

async def stream_chat(chat: list[dict], max_tokens: int | None = None,
                      temperature: float = 0.6):
    """Same request, yielded token by token.
    Retries like chat, but only until the first token — once the caller has
    seen output a transport error raises LLMError rather than restarting the
    answer from the top.
    Unparseable SSE chunks are skipped rather than raised
    """
    global _last_error
    payload = {
        "model": _resolved_model,
        "messages": chat,
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

# --- the agent's tool-aware stream ----------------------------------------

async def stream_agent_chat(chat: list[dict], tools: list | None = None,
                            max_tokens: int | None = None,
                            temperature: float = 0.6):
    """One agent turn over the wire, yielded as parts the loop can render.
    Each part is a dict: {"type": "content"|"reasoning", "text"} per token,
    then one {"type": "end", "finish_reason", "tool_calls"} record.
    tool_calls is the server's own list ([{id, type,
    function: {name, arguments}}], arguments a JSON string), or None when the
    turn carried no call. With tools=None the payload is an ordinary
    completion and the text-sentinel loop parses its answer instead.
    Retries like stream_chat, but only until the first part is seen
    """
    global _last_error
    payload = {
        "model": _resolved_model,
        "messages": chat,
        "temperature": temperature,
        "stream": True,
    }
    if tools is not None:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
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
                calls: dict = {}
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
                    ch = choices[0]
                    delta = ch.get("delta") or ch.get("message") or {}
                    text = delta.get("content")
                    if text:
                        started = True
                        yield {"type": "content", "text": str(text)}
                    reason = delta.get("reasoning_content") or delta.get("reasoning")
                    if reason:
                        started = True
                        yield {"type": "reasoning", "text": str(reason)}
                    for tc in delta.get("tool_calls") or []:
                        started = True
                        idx = tc.get("index", 0)
                        acc = calls.setdefault(idx, {"id": "", "type": "function",
                                                      "function": {"name": "", "arguments": ""}})
                        if tc.get("id"):
                            acc["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            acc["function"]["name"] = fn["name"]
                        if fn.get("arguments"):
                            acc["function"]["arguments"] += fn["arguments"]
                    if ch.get("finish_reason"):
                        yield {"type": "end", "finish_reason": ch["finish_reason"],
                               "tool_calls": list(calls.values()) or None}
                        _last_error = None
                        return
                yield {"type": "end", "finish_reason": None,
                       "tool_calls": list(calls.values()) or None}
                _last_error = None
                return
        except httpx.TransportError as e:
            _last_error = str(e)
            if started or attempt >= MAX_RETRIES:
                raise _llm_error("LLM stream failed mid-request", e)
            await _backoff(attempt - 1)
        except LLMError:
            raise

# --- tool-call transport detection ----------------------------------------

async def _probe_tool_mode() -> str:
    """One or two short completions deciding whether the server+model
    answer tool calls structurally ("native") or need them spelled out in
    the answer ("text").
    An error naming tools/functions/grammar is a refusal; a plain-text reply
    means text; a finish_reason of "tool_calls" means native. A run that
    runs out of tokens is inconclusive — a thinking model spends its budget
    reasoning before the call — so it retries once with a larger budget.
    Raises LLMError on a transport failure or an unrelated status, so a dead
    server does not silently flip the agent into text mode
    """
    for max_tokens in (64, 512):
        payload = {
            "model": _resolved_model,
            "messages": [{"role": "user",
                           "content": "Call the provided tool now and wait."}],
            "tools": [{"type": "function", "function": {
                "name": "probe_probe",
                "description": "A stub tool used to test whether tool calls "
                               "work. Call it and do nothing else.",
                "parameters": {"type": "object", "properties": {}}}}],
            "tool_choice": "auto",
            "max_tokens": max_tokens,
            "temperature": 0.0,
        }
        try:
            r = await get_client().post("chat/completions", json=payload,
                                        timeout=(10.0, 60.0))
        except httpx.TransportError as e:
            _last_error = str(e)
            raise _llm_error("Tool-mode probe could not reach the LLM server", e)
        if r.status_code >= 300:
            body = r.text.lower()
            if any(w in body for w in ("tool", "function", "grammar", "format")):
                return "text"
            raise _llm_error(f"LLM server returned HTTP {r.status_code}",
                             r.text[:300] or "")
        try:
            choice = (r.json().get("choices") or [{}])[0]
        except Exception as e:  # noqa: BLE001
            raise _llm_error("Malformed probe response from the LLM server", e)
        if (choice.get("finish_reason") == "tool_calls"
                or choice.get("message", {}).get("tool_calls")):
            return "native"
        if choice.get("finish_reason") != "length":
            return "text"
    return "text"

async def resolve_tool_mode() -> str:
    """The agent's tool-call transport: "native" (the server gets the tools
    array) or "text" (pythonic sentinel calls in the answer).
    A configured mode wins; otherwise one probe per model, cached until the
    resolved model changes. A probe the network fails on is not cached, so it
    is retried on the next call — mirroring is_loaded
    """
    global _tool_mode, _tool_mode_for
    mode = config.LLM_TOOL_MODE
    if mode != "auto":
        return mode
    async with _tool_mode_lock:
        if _tool_mode is None or _tool_mode_for != _resolved_model:
            try:
                _tool_mode = await _probe_tool_mode()
                _tool_mode_for = _resolved_model
            except LLMError:
                _tool_mode = None
                _tool_mode_for = ""
                return "text"
    return _tool_mode if _tool_mode is not None else "text"

# --- inline thinking-block splitting --------------------------------------

def _find_any(text: str, needles) -> tuple:
    """(position, needle) of the earliest needle in text, or (-1, None)
    """
    best, best_n = -1, None
    for n in needles:
        i = text.find(n)
        if i != -1 and (best == -1 or i < best):
            best, best_n = i, n
    return best, best_n

class ThinkingSplitter:
    """Splits a streamed assistant turn into trace/answer as the tokens land.
    The rule is the closed-segment one of split_thinking — a leading marker
    opens the trace, its matching close ends it — applied with a lookahead
    window, so a marker split across two tokens is still seen and a plain
    answer pops out as soon as the opening marker is ruled out. feed()
    returns the (role, text) pairs the token made ready; finish() the whole
    split
    """

    def __init__(self, markers=None):
        self._pairs = list(markers or config.LLM_THINKING_MARKERS)
        self._opens = [p[0] for p in self._pairs]
        self._closes = [p[1] for p in self._pairs]
        self._window = max((len(c) for c in self._closes), default=0)
        self._deferred = ""
        self._trace = ""
        self._answer = ""
        self._in_trace = False
        self._allow_trace = True
        self._close = None

    def _flush_answer(self):
        out = []
        if self._deferred:
            self._answer += self._deferred
            out.append(("answer", self._deferred))
            self._deferred = ""
        self._allow_trace = False
        return out

    def feed(self, token: str) -> list:
        """One more content token; [(role, text), ...] with roles trace/answer
        """
        out = []
        self._deferred += token
        while True:
            if self._in_trace:
                pos, needle = _find_any(self._deferred, [self._close])
                if pos != -1:
                    head, tail = self._deferred[:pos], self._deferred[pos + len(needle):]
                    self._deferred = tail
                    self._in_trace = False
                    self._allow_trace = False
                    if head:
                        self._trace += head
                        out.append(("trace", head))
                    continue
                if len(self._deferred) > 2 * self._window:
                    keep = len(self._deferred) - 2 * self._window
                    head, self._deferred = self._deferred[:keep], self._deferred[keep:]
                    self._trace += head
                    out.append(("trace", head))
                    continue
                return out
            if self._allow_trace and self._deferred:
                for open_, close in self._pairs:
                    if self._deferred.startswith(open_):
                        self._in_trace = True
                        self._close = close
                        self._deferred = self._deferred[len(open_):]
                        break
                if self._in_trace:
                    continue
                if any(o.startswith(self._deferred) for o in self._opens):
                    return out
            if not self._deferred:
                return out
            out.extend(self._flush_answer())
            return out

    def finish(self) -> tuple:
        """(trace, answer) — pending text flushed; trace is None when the turn
        never opened one
        """
        if self._deferred:
            if self._in_trace:
                self._trace += self._deferred
            else:
                self._answer += self._deferred
            self._deferred = ""
        return (self._trace or None), self._answer

def split_thinking(text: str, markers=None) -> tuple:
    """(trace, answer) — the leading thinking block of a finished assistant
    turn, split off by its marker pair.
    For each [open, close] pair in order: when the text starts with open, the
    span up to the first close is the trace and the rest the answer; no pair
    matches → (None, text), so a turn with no reasoning reads plain
    """
    for open_, close in markers or config.LLM_THINKING_MARKERS:
        if text.startswith(open_):
            i = text.find(close, len(open_))
            if i != -1:
                return text[len(open_):i], text[i + len(close):]
            return text[len(open_):], ""
    return None, text

def extract_json(text: str):
    """The JSON value buried in the model's answer, code fences stripped.
    Tries everything from the first bracket on, then the balanced span alone,
    so trailing prose does not spoil it.
    Raises ValueError when there is no bracket, or when neither candidate
    parses
    """
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
    """The balanced span opening at start, cut short at the first bracket that
    does not match.
    Strings and their escapes are stepped over, so a bracket inside a quote
    does not count
    """
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
    """Markdown for a page whose own text is too thin to keep.
    The input is truncated to config.WEB_MD_CHARS here, so the caller need not.
    Raises LLMError like any other chat call, which the fetcher reads as "no
    markdown" and falls back
    """
    system = load_prompt("web_to_markdown")
    user = ("Raw website content:\n=====\n"
            + str(content)[:config.WEB_MD_CHARS] + "\n=====")
    return await chat(system, user, max_tokens=config.WEB_MD_MAX_TOKENS,
                      temperature=0.3)
