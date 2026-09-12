"""The project agent: its tools, its loop, and its guardrails.

The model is stubbed — what is under test is the contract the panel and the
prompt rely on: a plain reply is streamed as assistant tokens and ends with
``done``; a tool call is written pythonically between sentinel tokens, is run
and fed back as a ``tool`` message instead of ever being shown as text; the
reasoning between <thinking> tags is split into the ``assistant-trace`` role;
and a tool cannot reach outside the project it was called for. The agent is
stateless — every run starts from the project's data alone.
"""

import io
import json

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


def call(name, **args):
    """A tool call as the model writes it: sentinel-wrapped and bracketed"""
    inner = name + "(" + ", ".join(f"{k}={v!r}" for k, v in args.items()) + ")"
    return ["<|tool_call_start|>", f"[{inner}]", "<|tool_call_end|>"]


class Replies(list):
    """The queue of stubbed model replies, carrying what the model was asked"""
    seen = ()


@pytest.fixture
def replies(monkeypatch):
    """Queues chunked model replies for llm.stream_chat, recording each chat.

    The loop matches sentinels token for token, so the stub yields each queued
    chunk as one token — the way a real stream would. `.seen` holds a copy of
    the conversation handed to the model on every call
    """
    from backend.integrations import llm

    queued, seen = Replies(), []

    async def fake_stream(messages, max_tokens=None, temperature=0.6):
        seen.append([dict(m) for m in messages])
        for chunk in (queued.pop(0) if queued else ["no reply queued"]):
            yield chunk

    monkeypatch.setattr(llm, "stream_chat", fake_stream)
    queued.seen = seen
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


# --- the loop -------------------------------------------------------------

def test_a_plain_reply_is_the_answer(client, replies):
    h = mkuser(client, "ag_plain")
    p = mkproject(client, h, "Plain")
    replies.append(["Nothing to look up here."])

    events = run_agent(client, h, p["id"], "hello")
    assert events[0] == {"type": "token", "role": "user", "text": "hello"}
    assert texts(events, "assistant") == "Nothing to look up here."
    assert events[-1]["type"] == "done"
    assert not toolcalls(events)
    # the model was handed exactly the system prompt and the new question
    assert [m["role"] for m in replies.seen[0]] == ["system", "user"]


def test_a_tool_call_is_run_and_fed_back_not_shown(client, replies):
    h = mkuser(client, "ag_call")
    p = mkproject(client, h, "Called")
    import_md(client, h, "Attention is all you need", "transformers", p["id"])
    replies.append(call("list_sources"))
    replies.append(["The project holds one paper."])

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

    replies.append(call("get_source_content", source_id=sid))
    replies.append(call("get_source_tags", source_id=sid))
    replies.append(["Read it."])
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

    replies.append(call("search_sources_by_title", title="alpha"))
    replies.append(["Found it."])
    events = run_agent(client, h, p["id"], "find alpha")
    by_title = toolcalls(events)[0]["result"]
    assert "Alpha paper" in by_title and "Beta paper" not in by_title

    replies.append(call("search_sources_by_tags", tags=["survey"]))
    replies.append(["Found it."])
    events = run_agent(client, h, p["id"], "find survey")
    by_tag = toolcalls(events)[0]["result"]
    assert "Alpha paper" in by_tag and "Beta paper" not in by_tag


def test_reasoning_is_split_out_and_kept_out_of_the_bubble(client, replies):
    h = mkuser(client, "ag_think")
    p = mkproject(client, h, "Think")
    replies.append(["<thinking>", "I should list them",
                    "</thinking>", *call("list_sources")])
    replies.append(["<thinking>", "Now I can answer", "</thinking>",
                    "Here is the list."])

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
    replies.append(["<|tool_call_start|>", "not a call at all",
                    "<|tool_call_end|>"])
    replies.append(["fine."])

    events = run_agent(client, h, p["id"], "go")
    (tc,) = toolcalls(events)
    assert "could not parse tool call" in tc["result"]
    assert texts(events, "assistant") == "fine."
    assert events[-1]["type"] == "done"


def test_an_unknown_tool_is_material_not_the_end(client, replies):
    h = mkuser(client, "ag_unknown")
    p = mkproject(client, h, "Unknown")
    replies.append(call("read_source", id="src_deadbeef"))
    replies.append(["No such tool."])

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


# --- the guardrail --------------------------------------------------------

def test_the_agent_only_sees_its_own_project(client, replies):
    h = mkuser(client, "ag_walls")
    mine = mkproject(client, h, "Mine")
    theirs = mkproject(client, h, "Theirs")
    import_md(client, h, "Ours", "our text", mine["id"])
    secret = import_md(client, h, "Secret", "the secret text", theirs["id"])

    replies.append(call("list_sources"))
    replies.append(["done"])
    events = run_agent(client, h, mine["id"], "list")
    listing = toolcalls(events)[0]["result"]
    assert "Ours" in listing and "Secret" not in listing

    # an id borrowed from another project is refused, and its text never
    # reaches either the thread or the model's next context
    replies.append(call("get_source_content", source_id=secret))
    replies.append(["refused"])
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


# --- statelessness --------------------------------------------------------

def test_each_run_starts_fresh(client, replies):
    h = mkuser(client, "ag_fresh")
    p = mkproject(client, h, "Fresh")
    replies.append(["first answer"])
    run_agent(client, h, p["id"], "first question")
    replies.append(["second answer"])
    run_agent(client, h, p["id"], "second question")

    # nothing of the first run survives into the second: its first call is
    # system + the new question, nothing else
    assert [m["role"] for m in replies.seen[-1]] == ["system", "user"]
    assert replies.seen[-1][-1]["content"] == "second question"


def test_a_dead_llm_is_an_error_event(client, replies, monkeypatch):
    from backend.integrations import llm

    async def dead(messages, max_tokens=None, temperature=0.6):
        raise llm.LLMError("connection refused")
        yield  # unreachable; the yield makes this an async generator, like stream_chat

    monkeypatch.setattr(llm, "stream_chat", dead)
    h = mkuser(client, "ag_dead")
    p = mkproject(client, h, "Dead")
    events = run_agent(client, h, p["id"], "hi")
    assert events[-1]["type"] == "error"
    assert "connection refused" in events[-1]["error"]