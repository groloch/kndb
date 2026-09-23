"""The project agent: its tools, its loop, and its guardrails.

The model is stubbed — what is under test is the contract the panel and the
prompt rely on: a plain reply is streamed as assistant tokens and ends with
``done``; a tool call travels natively (the server's tool_calls) or
pythonically between sentinel tokens, is run and fed back as a ``tool``
message instead of ever being shown as text; the reasoning is split into the
``assistant-trace`` role whether the server ships it as reasoning_content or
inline between markers; and a tool cannot reach outside the project it was
called for. The agent remembers per (project, user) session: a run continues
the caller's previous exchange, other users and cleared sessions do not, and
a failed run writes nothing (content.sessions).
"""

import asyncio
import io
import json

import httpx
import pytest


def mkuser(client, name):
    client.post("/api/users", json={"name": name})
    return {"X-KNDB-User": name}


def mkproject(client, headers, name):
    r = client.post("/api/projects", json={"name": name}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["project"]


def import_md(client, headers, title, body, project, tags=""):
    files = {"file": (f"{title}.md", io.BytesIO(body.encode()), "text/markdown")}
    r = client.post("/api/import", data={"project": project}, files=files,
                    headers=headers)
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    if tags:
        client.post(f"/api/source/{sid}/meta", json={"tags": tags}, headers=headers)
    return sid


def tokens(*parts):
    """A plain turn: content chunks (one list entry per part), then the end"""
    return ([{"type": "content", "text": p} for p in parts]
            + [{"type": "end", "finish_reason": "stop", "tool_calls": None}])


def reasoning(*parts):
    """A turn whose thinking the server shipped as reasoning_content"""
    return ([{"type": "reasoning", "text": p} for p in parts]
            + [{"type": "end", "finish_reason": "stop", "tool_calls": None}])


def text_call(name, **args):
    """One pythonic call as the model writes it in text mode: sentinel-wrapped
    and bracketed
    """
    inner = name + "(" + ", ".join(f"{k}={v!r}" for k, v in args.items()) + ")"
    return "<|tool_call_start|>[" + inner + "]<|tool_call_end|>"


def native_calls(*calls):
    """A native tool-call turn, as the server would stream it.
    Each call is an (id, name, arguments) triple; arguments a plain dict that
    becomes the JSON arguments string
    """
    records = [{"id": cid, "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}
               for cid, name, args in calls]
    return [{"type": "end", "finish_reason": "tool_calls", "tool_calls": records}]


class Replies(list):
    """The queue of stubbed model turns, carrying what the model was asked"""
    seen = ()
    tools_seen = ()
    mode = "text"


@pytest.fixture
def replies(monkeypatch):
    """Queues chunked model turns for llm.stream_agent_chat, recording each
    conversation and the tools array sent with it. `.seen` holds a copy of
    the conversation handed to the model on every call, `.tools_seen` the
    tools payload of each, and `.mode` picks the transport the loop resolves
    (helpers emit sentinel text calls, so the default is "text")
    """
    from backend.integrations import llm

    queued, seen, tools_seen = Replies(), [], []

    async def fake_stream_agent_chat(messages, tools=None, max_tokens=None,
                                     temperature=0.6):
        seen.append([dict(m) for m in messages])
        tools_seen.append(tools)
        for chunk in (queued.pop(0) if queued
                      else [{"type": "end", "finish_reason": "stop",
                             "tool_calls": None}]):
            yield chunk

    async def fake_resolve_tool_mode():
        return queued.mode

    monkeypatch.setattr(llm, "resolve_tool_mode", fake_resolve_tool_mode)
    monkeypatch.setattr(llm, "stream_agent_chat", fake_stream_agent_chat)
    queued.seen = seen
    queued.tools_seen = tools_seen
    return queued


def run_agent(client, headers, pid, text):
    """One agent run, as the list of events it streamed
    """
    with client.stream("POST", f"/api/agent/message/{pid}",
                       json={"message": text}, headers=headers) as r:
        assert r.status_code == 200, r.read()
        out = []
        for line in r.iter_lines():
            if line.startswith("data:"):
                out.append(json.loads(line[5:].strip()))
    return out


def texts(events, *roles):
    """The streamed text, optionally of given roles only
    """
    return "".join(e["text"] for e in events
                   if e["type"] == "token" and (not roles or e["role"] in roles))


def toolcalls(events):
    return [e for e in events if e["type"] == "toolcall"]


# --- the tool table -------------------------------------------------------

def test_the_tool_spec_and_the_functions_agree(client):
    """prepare_tools is the single source of truth: a tool cannot exist in the
    prompt without a function to run it, or be described otherwise
    """
    from backend.content.agent import prepare_tools

    tools_json, functions = prepare_tools("prj_x")
    tools = json.loads(tools_json)
    assert [t["name"] for t in tools] == [
        "list_sources", "list_sources_tags", "search_sources_by_title",
        "search_sources_by_tags", "get_source_content", "get_source_tags",
    ]
    assert set(functions) == {t["name"] for t in tools}
    assert all(t["description"] and t["parameters"] for t in tools)
    # native mode hands back the same schemas wrapped for the tools array
    tools_list, functions = prepare_tools("prj_x", native=True)
    assert [t["function"] for t in tools_list] == tools
    assert all(t["type"] == "function" for t in tools_list)


def test_parse_pythonic_toolcall():
    from backend.content.agent import parse_pythonic_toolcall

    name, args = parse_pythonic_toolcall('[search_sources_by_title(title="alpha")]')
    assert name == "search_sources_by_title"
    assert args == {"title": "alpha"}
    name, args = parse_pythonic_toolcall(
        '[search_sources_by_tags(tags=["ml", "survey"], exact=False)]')
    assert name == "search_sources_by_tags"
    assert args == {"tags": ["ml", "survey"], "exact": False}


def test_parse_json_toolcall():
    from backend.content.agent import parse_json_toolcall

    name, args = parse_json_toolcall('{"name": "list_sources", "arguments": {}}')
    assert name == "list_sources"
    assert args == {}


def test_normalize_native_toolcall():
    from backend.content.agent import normalize_native_toolcall

    name, args, cid = normalize_native_toolcall({
        "id": "c1", "type": "function",
        "function": {"name": "search_sources_by_title",
                     "arguments": '{"title": "alpha"}'}})
    assert (name, args, cid) == ("search_sources_by_title", {"title": "alpha"}, "c1")
    # a mangled arguments string is tolerated, not fatal
    name, args, _ = normalize_native_toolcall({
        "id": "c2", "type": "function",
        "function": {"name": "list_sources", "arguments": "not json at all"}})
    assert name == "list_sources"
    assert args == {}


# --- the loop, text transport (pythonic sentinel calls) --------------------

def test_a_plain_reply_is_the_answer(client, replies):
    h = mkuser(client, "ag_plain")
    p = mkproject(client, h, "Plain")
    replies.append(tokens("Nothing to look up here."))

    events = run_agent(client, h, p["id"], "hello")
    assert events[0] == {"type": "token", "role": "user", "text": "hello"}
    assert texts(events, "assistant") == "Nothing to look up here."
    assert events[-1]["type"] == "done"
    assert not toolcalls(events)
    # the model was handed exactly the system prompt and the new question
    assert [m["role"] for m in replies.seen[0]] == ["system", "user"]
    # text mode sends no tools array — the answer is parsed for calls instead
    assert replies.tools_seen[0] is None


def test_a_tool_call_is_run_and_fed_back_not_shown(client, replies):
    h = mkuser(client, "ag_call")
    p = mkproject(client, h, "Called")
    import_md(client, h, "Attention is all you need", "transformers", p["id"])
    replies.append(tokens(text_call("list_sources")))
    replies.append(tokens("The project holds one paper."))

    events = run_agent(client, h, p["id"], "what is in here?")
    (tc,) = toolcalls(events)
    assert tc["tool"] == "list_sources"
    assert tc["args"] == {}
    assert "Attention is all you need" in tc["result"]
    # the call itself never reaches the thread
    assert texts(events, "assistant") == "The project holds one paper."

    # and the tool's answer is fed back as a tool message, not as text
    fed = replies.seen[1]
    assert [m["role"] for m in fed] == ["system", "user", "assistant", "tool"]
    assert "Attention is all you need" in fed[-1]["content"]


def test_pythonic_calls_carry_their_arguments(client, replies):
    h = mkuser(client, "ag_written")
    p = mkproject(client, h, "Written")
    sid = import_md(client, h, "Paper", "the body of the paper", p["id"],
                    tags="ml, survey")

    replies.append(tokens(text_call("get_source_content", source_id=sid)))
    replies.append(tokens(text_call("get_source_tags", source_id=sid)))
    replies.append(tokens("Read it."))
    events = run_agent(client, h, p["id"], "read the paper")

    content, tags = [tc["result"] for tc in toolcalls(events)]
    assert "the body of the paper" in content
    assert "ml" in tags and "survey" in tags
    assert "the body of the paper" in replies.seen[1][-1]["content"]


def test_searching_by_title_and_by_tags(client, replies):
    h = mkuser(client, "ag_search")
    p = mkproject(client, h, "Search")
    import_md(client, h, "Alpha paper", "a", p["id"], tags="ml, survey")
    import_md(client, h, "Beta paper", "b", p["id"], tags="ml")

    replies.append(tokens(text_call("search_sources_by_title", title="alpha")))
    replies.append(tokens("Found it."))
    events = run_agent(client, h, p["id"], "find alpha")
    by_title = toolcalls(events)[0]["result"]
    assert "Alpha paper" in by_title and "Beta paper" not in by_title

    replies.append(tokens(text_call("search_sources_by_tags", tags=["survey"])))
    replies.append(tokens("Found it."))
    events = run_agent(client, h, p["id"], "find survey")
    by_tag = toolcalls(events)[0]["result"]
    assert "Alpha paper" in by_tag and "Beta paper" not in by_tag


def test_reasoning_is_split_out_and_kept_out_of_the_bubble(client, replies):
    h = mkuser(client, "ag_think")
    p = mkproject(client, h, "Think")
    replies.append(tokens("<thinking>I should list them</thinking>"
                          + text_call("list_sources")))
    replies.append(tokens("<thinking>Now I can answer</thinking>"
                          + "Here is the list."))

    events = run_agent(client, h, p["id"], "list them")
    # the reasoning travels as its own role, the answer as the assistant's
    assert texts(events, "assistant-trace") == "I should list themNow I can answer"
    assert texts(events, "assistant") == "Here is the list."
    # …but the raw trace, tags and all, is what the model sees again
    trace = replies.seen[1][-2]["content"]
    assert trace == ("<thinking>I should list them</thinking>"
                     "<|tool_call_start|>[list_sources()]<|tool_call_end|>")


def test_a_parse_failure_is_material_not_the_end(client, replies):
    h = mkuser(client, "ag_garbage")
    p = mkproject(client, h, "Garbage")
    replies.append(tokens("<|tool_call_start|>not a call at all<|tool_call_end|>"))
    replies.append(tokens("fine."))

    events = run_agent(client, h, p["id"], "go")
    (tc,) = toolcalls(events)
    assert "could not parse tool call" in tc["result"]
    assert texts(events, "assistant") == "fine."
    assert events[-1]["type"] == "done"


def test_an_unknown_tool_is_material_not_the_end(client, replies):
    h = mkuser(client, "ag_unknown")
    p = mkproject(client, h, "Unknown")
    replies.append(tokens(text_call("read_source", id="src_deadbeef")))
    replies.append(tokens("No such tool."))

    events = run_agent(client, h, p["id"], "read src_deadbeef")
    (tc,) = toolcalls(events)
    assert tc["result"] == "unknown tool: read_source"
    assert events[-1]["type"] == "done"


def test_the_loop_stops_when_a_reply_carries_no_call(client, replies):
    """No sentinels at all — the whole reply is text and the run is done"""
    h = mkuser(client, "ag_quiet")
    p = mkproject(client, h, "Quiet")
    events = run_agent(client, h, p["id"], "hello?")  # nothing queued
    assert events[-1]["type"] == "done"
    assert not toolcalls(events)


def test_a_text_reply_streams_token_by_token(client, replies):
    """The turn streams as it lands: one token event per chunk, not one block
    once the turn is over
    """
    h = mkuser(client, "ag_stream")
    p = mkproject(client, h, "Stream")
    replies.append(tokens("alpha ", "beta ", "gamma."))
    events = run_agent(client, h, p["id"], "go")
    pieces = [e["text"] for e in events
              if e["type"] == "token" and e["role"] == "assistant"]
    assert pieces == ["alpha ", "beta ", "gamma."]
    assert events[-1]["type"] == "done"


def test_a_native_reply_streams_token_by_token(client, replies):
    h = mkuser(client, "ag_nstream")
    p = mkproject(client, h, "NStream")
    replies.mode = "native"
    replies.append(tokens("one ", "two"))
    events = run_agent(client, h, p["id"], "go")
    pieces = [e["text"] for e in events
              if e["type"] == "token" and e["role"] == "assistant"]
    assert pieces == ["one ", "two"]
    assert events[-1]["type"] == "done"


def test_a_sentinel_mid_answer_never_reaches_the_stream(client, replies):
    """A pythonic call riding between prose is swallowed live: the words
    before it stream as they land, the sentinel never shows as text, and the
    call still runs and streams its result
    """
    h = mkuser(client, "ag_noleak")
    p = mkproject(client, h, "NoLeak")
    sid = import_md(client, h, "Paper", "the body of the paper", p["id"])
    replies.append(tokens("Let me look. ",
                          text_call("get_source_content", source_id=sid)))
    replies.append(tokens("Here it is."))
    events = run_agent(client, h, p["id"], "read the paper")

    for e in events:
        if e["type"] == "token":
            assert "<|" not in e["text"]
    assert texts(events, "assistant") == "Let me look. Here it is."
    (tc,) = toolcalls(events)
    assert "the body of the paper" in tc["result"]


# --- the loop, native transport (server tool_calls) ------------------------

def test_native_tool_call_transport(client, replies):
    h = mkuser(client, "ag_native")
    p = mkproject(client, h, "Native")
    import_md(client, h, "Paper", "the body", p["id"])
    replies.mode = "native"
    replies.append(native_calls(("call_1", "list_sources", {})))
    replies.append(tokens("Got it."))

    events = run_agent(client, h, p["id"], "what's here?")
    (tc,) = toolcalls(events)
    assert tc["tool"] == "list_sources"
    assert tc["args"] == {}
    assert "Paper" in tc["result"]
    assert texts(events, "assistant") == "Got it."

    # the tools array went over the wire, the assistant turn came back with
    # its tool_calls echoed, and each result rode its own call id
    assert [t["function"]["name"] for t in replies.tools_seen[0]] == [
        "list_sources", "list_sources_tags", "search_sources_by_title",
        "search_sources_by_tags", "get_source_content", "get_source_tags"]
    fed = replies.seen[1]
    assert fed[-2]["role"] == "assistant"
    assert fed[-2]["tool_calls"][0]["function"]["name"] == "list_sources"
    assert fed[-2]["tool_calls"][0]["id"] == "call_1"
    assert fed[-1]["role"] == "tool"
    assert fed[-1]["tool_call_id"] == "call_1"
    assert "Paper" in fed[-1]["content"]
    # the native wording, not the pythonic one, reached the prompt
    assert "pythonic" not in replies.seen[0][0]["content"]


def test_native_reasoning_is_streamed_as_trace(client, replies):
    h = mkuser(client, "ag_native_trace")
    p = mkproject(client, h, "NativeTrace")
    replies.mode = "native"
    replies.append([
        {"type": "reasoning", "text": "The user wants a list."},
        {"type": "reasoning", "text": "Let me call."},
        {"type": "end", "finish_reason": "tool_calls", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "list_sources", "arguments": "{}"}}]},
    ])
    replies.append(reasoning("They asked about tags.") + [
        {"type": "content", "text": "Those are the tags."},
        {"type": "end", "finish_reason": "stop", "tool_calls": None},
    ])

    events = run_agent(client, h, p["id"], "go")
    assert texts(events, "assistant-trace") == \
        "The user wants a list.Let me call.They asked about tags."
    assert texts(events, "assistant") == "Those are the tags."

    # the reasoning is fed back as reasoning_content, not inline — the shape
    # the server's template reads it back from
    fed = replies.seen[1][-2]
    assert fed["role"] == "assistant"
    assert fed["reasoning_content"] == "The user wants a list.Let me call."
    assert "The user wants a list." not in fed["content"]


def test_native_inline_thinking_is_split_like_text(client, replies):
    """lmf-style ` thinking` … ` response` blocks ride inline in the content
    even over the native transport — the splitter still pulls them out
    """
    h = mkuser(client, "ag_lfm")
    p = mkproject(client, h, "Lfm")
    replies.mode = "native"
    replies.append([
        {"type": "content", "text": " thinking The user wants a list.\n"
                                      " response\nHere it is."},
        {"type": "end", "finish_reason": "tool_calls", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "list_sources", "arguments": "{}"}}]},
    ])
    replies.append(tokens(" thought again Done."))

    events = run_agent(client, h, p["id"], "go")
    # the space the model wrote after the marker stays part of the trace
    assert texts(events, "assistant-trace") == " The user wants a list.\n"
    assert texts(events, "assistant") == "\nHere it is." + " thought again Done."


def test_native_multiple_calls_are_run_in_order(client, replies):
    h = mkuser(client, "ag_multi")
    p = mkproject(client, h, "Multi")
    sid = import_md(client, h, "Alpha paper", "a", p["id"], tags="ml, survey")
    replies.mode = "native"
    replies.append(native_calls(
        ("c1", "search_sources_by_title", {"title": "alpha"}),
        ("c2", "get_source_tags", {"source_id": sid}),
    ))
    replies.append(tokens("Found them."))

    events = run_agent(client, h, p["id"], "find alpha")
    first, second = toolcalls(events)
    assert first["tool"] == "search_sources_by_title"
    assert "Alpha paper" in first["result"]
    assert second["tool"] == "get_source_tags"
    assert "ml" in second["result"] and "survey" in second["result"]

    fed = replies.seen[1]
    assert [m["role"] for m in fed] == ["system", "user", "assistant", "tool", "tool"]
    assert fed[-2]["tool_call_id"] == "c1"
    assert fed[-1]["tool_call_id"] == "c2"


# --- the thinking splitter -------------------------------------------------

def test_split_thinking_closed_segments():
    from backend.integrations import llm

    trace, answer = llm.split_thinking("<thinking>hmm</thinking>hello")
    assert (trace, answer) == ("hmm", "hello")
    trace, answer = llm.split_thinking(" thinking let me think\n response\nok")
    assert (trace, answer) == (" let me think\n", "\nok")
    # lfm2.5 sometimes truncates the marker to "<think>" instead of
    # "<thinking>" — the pair still ends at the bare word "response"
    trunc = "<" + "think" + ">"
    trace, answer = llm.split_thinking(trunc + " let me think\nresponse\nok")
    assert (trace, answer) == (" let me think\n", "\nok")
    # no marker at the start — a plain answer, untouched
    trace, answer = llm.split_thinking("just an answer")
    assert trace is None and answer == "just an answer"
    # a marker that never closes leaves everything as the trace
    trace, answer = llm.split_thinking("<thinking>never closes")
    assert trace == "never closes" and answer == ""


def test_thought_splitter_handles_fragmented_markers():
    from backend.integrations import llm

    s = llm.ThinkingSplitter()
    chunks = []
    for piece in ["<th", "inking>I", " shou", "ld think</think", "ing>ready"]:
        chunks += s.feed(piece)
    assert s.finish() == ("I should think", "ready")
    assert chunks == [("trace", "I should think"), ("answer", "ready")]

    # the lfm markers, split mid-marker: the trace is withheld only until the
    # opening marker is confirmed, then flushed out live
    s = llm.ThinkingSplitter()
    chunks = []
    for piece in [" th", "inking hmm", "\n\n resp", "onse\nhello"]:
        chunks += s.feed(piece)
    assert s.finish() == (" hmm\n\n", "\nhello")
    assert chunks == [("trace", " hmm\n\n"), ("answer", "\nhello")]


def test_thought_splitter_streams_a_plain_answer_through(client):
    """No marker: every token comes straight out as answer, unchanged"""
    from backend.integrations import llm

    s = llm.ThinkingSplitter()
    out = []
    for piece in ["The ", "answer ", "is 4."]:
        out += s.feed(piece)
    assert s.finish() == (None, "The answer is 4.")
    assert out == [("answer", "The "), ("answer", "answer "), ("answer", "is 4.")]


def test_thought_splitter_swallows_dropped_spans(client):
    """Dropped spans — the tool-call sentinels of text mode — never reach
    trace or answer, even split across tokens; text around them streams
    through as it lands
    """
    from backend.integrations import llm

    s = llm.ThinkingSplitter(drops=[("<|c|>", "<|/c|>")])
    out = []
    for piece in ["a ", "<|", "c|>swallow", "ed<|/c|> b"]:
        out += s.feed(piece)
    out += s.spill()
    assert out == [("answer", "a "), ("answer", " b")]
    assert s.finish() == (None, "a  b")

    # a drop that never closes is discarded at end of stream, its opening
    # held-back prefix too
    s = llm.ThinkingSplitter(drops=[("<|c|>", "<|/c|>")])
    out = []
    for piece in ["ans ", "<|c|> oops"]:
        out += s.feed(piece)
    assert out == [("answer", "ans ")]
    assert s.finish() == (None, "ans ")

    # text before a dropped span closes off any later trace, like in
    # split_thinking
    s = llm.ThinkingSplitter(drops=[("<|c|>", "<|/c|>")])
    out = []
    for piece in ["Sure. ", "<|c|>x<|/c|>", "<thinking>late</thinking>"]:
        out += s.feed(piece)
    out += s.spill()
    assert out == [("answer", "Sure. "), ("answer", "<thinking>late</thinking>")]
    assert s.finish() == (None, "Sure. <thinking>late</thinking>")


# --- mode resolution ------------------------------------------------------

class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def _probe_client(posts, resp):
    class _Client:
        async def post(self, url, json=None, timeout=None):
            posts.append(url)
            return resp
    return _Client()


def test_resolve_tool_mode_probes_and_caches(monkeypatch):
    from backend.integrations import llm

    monkeypatch.setattr(llm, "_tool_mode", None)
    monkeypatch.setattr(llm, "_tool_mode_for", "")
    posts = []
    monkeypatch.setattr(llm, "get_client", lambda: _probe_client(
        posts, _Resp(200, {"choices": [
            {"finish_reason": "tool_calls", "message": {
                "tool_calls": [{"id": "x", "type": "function",
                                "function": {"name": "probe_probe",
                                             "arguments": "{}"}}]}}]})))
    assert asyncio.run(llm.resolve_tool_mode()) == "native"
    assert asyncio.run(llm.resolve_tool_mode()) == "native"
    assert len(posts) == 1  # cached after the first probe


def test_resolve_tool_mode_falls_back_to_text(monkeypatch):
    from backend.integrations import llm

    monkeypatch.setattr(llm, "_tool_mode", None)
    monkeypatch.setattr(llm, "_tool_mode_for", "")
    posts = []
    # the server answers with plain text — no structural call
    monkeypatch.setattr(llm, "get_client", lambda: _probe_client(
        posts, _Resp(200, {"choices": [{"finish_reason": "stop",
                                        "message": {"content": "no"}}]})))
    assert asyncio.run(llm.resolve_tool_mode()) == "text"
    # and so does an error that calls the tools/format out
    monkeypatch.setattr(llm, "_tool_mode", None)
    monkeypatch.setattr(llm, "_tool_mode_for", "")
    monkeypatch.setattr(llm, "get_client", lambda: _probe_client(
        posts, _Resp(501, text="function calling is not supported by this model")))
    assert asyncio.run(llm.resolve_tool_mode()) == "text"
    assert len(posts) == 2


def test_resolve_tool_mode_retries_until_the_server_answers(monkeypatch):
    from backend.integrations import llm

    monkeypatch.setattr(llm, "_tool_mode", None)
    monkeypatch.setattr(llm, "_tool_mode_for", "")
    posts = []

    class _Flaky:
        def __init__(self):
            self._up = False

        async def post(self, url, json=None, timeout=None):
            posts.append(url)
            if not self._up:
                raise httpx.ConnectError("connection refused")
            return _Resp(200, {"choices": [{"finish_reason": "tool_calls",
                                            "message": {}}]})

    flaky = _Flaky()
    monkeypatch.setattr(llm, "get_client", lambda: flaky)
    # down: falls back to text for the call, caches nothing
    assert asyncio.run(llm.resolve_tool_mode()) == "text"
    # up next time: the probe runs again and finds native
    flaky._up = True
    assert asyncio.run(llm.resolve_tool_mode()) == "native"
    assert len(posts) == 2


def test_resolve_tool_mode_obeys_the_configured_override(monkeypatch):
    from backend.integrations import llm
    from backend.core import config

    monkeypatch.setattr(config, "LLM_TOOL_MODE", "native")
    posts = []

    class _Dead:
        async def post(self, url, json=None, timeout=None):
            posts.append(url)
            raise AssertionError("the probe must not run under an override")

    monkeypatch.setattr(llm, "get_client", lambda: _Dead())
    assert asyncio.run(llm.resolve_tool_mode()) == "native"
    assert posts == []


# --- the guardrail --------------------------------------------------------

def test_the_agent_only_sees_its_own_project(client, replies):
    h = mkuser(client, "ag_walls")
    mine = mkproject(client, h, "Mine")
    theirs = mkproject(client, h, "Theirs")
    import_md(client, h, "Ours", "our text", mine["id"])
    secret = import_md(client, h, "Secret", "the secret text", theirs["id"])

    replies.append(tokens(text_call("list_sources")))
    replies.append(tokens("done"))
    events = run_agent(client, h, mine["id"], "list")
    listing = toolcalls(events)[0]["result"]
    assert "Ours" in listing and "Secret" not in listing

    # an id borrowed from another project is refused, and its text never
    # reaches either the thread or the model's next context
    replies.append(tokens(text_call("get_source_content", source_id=secret)))
    replies.append(tokens("refused"))
    events = run_agent(client, h, mine["id"], "read the other one")
    (tc,) = toolcalls(events)
    assert "not in this project" in tc["result"]
    assert "the secret text" not in tc["result"]
    assert "the secret text" not in replies.seen[1][-1]["content"]


def test_only_members_may_talk_to_the_agent(client):
    owner = mkuser(client, "ag_owner")
    stranger = mkuser(client, "ag_stranger")
    p = mkproject(client, owner, "Closed")
    r = client.post(f"/api/agent/message/{p['id']}",
                    json={"message": "hi"}, headers=stranger)
    assert r.status_code == 403


# --- per-session memory ---------------------------------------------------

def test_a_run_continues_the_previous_one(client, replies):
    """The next run starts from the system prompt and the previous exchange,
    not from the new question alone
    """
    h = mkuser(client, "ag_memory")
    p = mkproject(client, h, "Memory")
    replies.append(tokens("first answer"))
    run_agent(client, h, p["id"], "first question")
    replies.append(tokens("second answer"))
    run_agent(client, h, p["id"], "second question")

    conv = replies.seen[-1]
    assert [m["role"] for m in conv] == ["system", "user", "assistant", "user"]
    assert conv[1]["content"] == "first question"
    assert conv[2]["content"] == "first answer"
    assert conv[3]["content"] == "second question"


def test_tool_traffic_rides_the_history_too(client, replies):
    """The exchange a tool call produced — assistant with its tool_calls and
    the tool answers — is history like any other turn
    """
    h = mkuser(client, "ag_hist_tool")
    p = mkproject(client, h, "HistTool")
    import_md(client, h, "Paper", "the body", p["id"])

    replies.mode = "native"
    replies.append(native_calls(("call_1", "list_sources", {})))
    replies.append(tokens("first answer"))
    run_agent(client, h, p["id"], "first question")
    replies.append(tokens("second answer"))
    run_agent(client, h, p["id"], "second question")

    conv = replies.seen[-1]
    assert [m["role"] for m in conv] == [
        "system", "user", "assistant", "tool", "assistant", "user"]
    assert conv[2]["tool_calls"][0]["function"]["name"] == "list_sources"
    assert conv[3]["tool_call_id"] == "call_1"
    assert conv[4]["content"] == "first answer"
    assert conv[5]["content"] == "second question"


def test_sessions_are_per_user(client, replies):
    """Two members of one project converse separately — neither's history
    leaks into the other's context
    """
    a = mkuser(client, "ag_user_a")
    b = mkuser(client, "ag_user_b")
    p = mkproject(client, a, "Shared")
    r = client.post(f"/api/projects/{p['id']}/members",
                    json={"name": "ag_user_b", "role": "contributor"},
                    headers=a)
    assert r.status_code == 200, r.text
    replies.append(tokens("answer to a"))
    run_agent(client, a, p["id"], "question from a")
    replies.append(tokens("answer to b"))
    run_agent(client, b, p["id"], "question from b")

    conv = replies.seen[-1]
    assert [m["role"] for m in conv] == ["system", "user"]
    assert conv[-1]["content"] == "question from b"


def test_a_failed_run_writes_no_history(client, replies, monkeypatch):
    """A run that dies mid-stream leaves the session as it was: no partial
    answer, no half-finished tool turn becomes history
    """
    from backend.integrations import llm

    h = mkuser(client, "ag_failed")
    p = mkproject(client, h, "Failed")
    replies.append(tokens("good answer"))
    run_agent(client, h, p["id"], "good question")

    async def dead(messages, tools=None, max_tokens=None, temperature=0.6):
        raise llm.LLMError("connection refused")
        yield  # unreachable; the yield makes this an async generator

    # wrap the fixture's stub, so the failure can be switched off again
    # without undoing the fixture's own patches
    stub = llm.stream_agent_chat
    failing = True

    async def flaky(*a, **k):
        if failing:
            raise llm.LLMError("connection refused")
            yield  # unreachable
        async for chunk in stub(*a, **k):
            yield chunk

    monkeypatch.setattr(llm, "stream_agent_chat", flaky)
    events = run_agent(client, h, p["id"], "doomed question")
    assert events[-1]["type"] == "error"

    failing = False
    replies.append(tokens("recovered"))
    run_agent(client, h, p["id"], "after the failure")

    conv = replies.seen[-1]
    assert [m["role"] for m in conv] == ["system", "user", "assistant", "user"]
    assert conv[1]["content"] == "good question"
    assert conv[3]["content"] == "after the failure"


def test_the_session_replays_and_resets(client, replies):
    """A reloaded page redraws the thread from the session's display log;
    deleting the session forgets both the log and the history
    """
    h = mkuser(client, "ag_replay")
    p = mkproject(client, h, "Replay")
    import_md(client, h, "Paper", "the body", p["id"])
    replies.append(tokens(text_call("list_sources")))
    replies.append(tokens("found it"))
    run_agent(client, h, p["id"], "list them")

    r = client.get(f"/api/agent/session/{p['id']}", headers=h)
    assert r.status_code == 200
    events = r.json()["events"]
    kinds = [(e["type"], e.get("role")) for e in events]
    assert ("token", "user") in kinds
    assert ("toolcall", None) in kinds
    assert ("token", "assistant") in kinds
    assert texts([e for e in events if e["type"] == "token"],
                 "assistant") == "found it"
    assert not [e for e in events if e["type"] in ("done", "error")]

    r = client.delete(f"/api/agent/session/{p['id']}", headers=h)
    assert r.status_code == 200
    assert client.get(f"/api/agent/session/{p['id']}",
                      headers=h).json()["events"] == []

    replies.append(tokens("after reset"))
    run_agent(client, h, p["id"], "hello again")
    assert [m["role"] for m in replies.seen[-1]] == ["system", "user"]


def test_the_session_routes_need_membership(client):
    owner = mkuser(client, "ag_sess_owner")
    stranger = mkuser(client, "ag_sess_stranger")
    p = mkproject(client, owner, "SessClosed")
    assert client.get(f"/api/agent/session/{p['id']}",
                      headers=stranger).status_code == 403
    assert client.delete(f"/api/agent/session/{p['id']}",
                         headers=stranger).status_code == 403


def test_a_dead_llm_is_an_error_event(client, replies, monkeypatch):
    from backend.integrations import llm

    async def dead(messages, tools=None, max_tokens=None, temperature=0.6):
        raise llm.LLMError("connection refused")
        yield  # unreachable; the yield makes this an async generator

    monkeypatch.setattr(llm, "stream_agent_chat", dead)
    h = mkuser(client, "ag_dead")
    p = mkproject(client, h, "Dead")
    events = run_agent(client, h, p["id"], "hi")
    assert events[-1]["type"] == "error"
    assert "connection refused" in events[-1]["error"]