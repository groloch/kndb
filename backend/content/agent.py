"""The agent's tool-side plumbing: the catalogue the ``tools`` package
declares, prepared per run for the loop, and the parsers turning whatever
the model emitted back into a name and arguments.
"""

import ast
import json

from .tools import TOOLS as _TOOLS
from ..integrations.llm import extract_json


def tool_dicts() -> list:
    """The schemas the LLM is offered
    """
    return [t["tool_dict"] for t in _TOOLS]


def tool_security() -> dict:
    """Tool name → the security level it declares. The guard reads it; the
    LLM never does — the level lives on the outer dict, not inside
    ``tool_dict``, so it is server-side only
    """
    return {t["tool_dict"]["name"]: t.get("security", "admin") for t in _TOOLS}


def prepare_tools(pid: str, user: str = "", native: bool = False):
    """The whole catalogue bound for one run: the schema list (JSON string
    under the text contract, native list under the native one) and the
    callables, each with the run's ``pid`` bound as first argument, and the
    calling ``user`` as second when the tool writes as them
    """
    return _bind(_TOOLS, pid, user, native)


async def select_tools(pid: str, user: str = "", native: bool = False):
    """The tools for one run: the catalogue filtered by each tool's per-
    project availability gate, then bound like ``prepare_tools``. Returns
    the schema list, the callables, and the security map of the tools that
    actually went in — one source of truth for the prompt, the functions and
    the guard. An unavailable tool is unknown to the run: it is in no
    schema, and a call to it is refused on the spot
    """
    entries = [t for t in _TOOLS
               if t.get("available") is None or await t["available"](pid)]
    tools, functions = _bind(entries, pid, user, native)
    security = {t["tool_dict"]["name"]: t.get("security", "admin")
                for t in entries}
    return tools, functions, security


def _bind(entries: tuple, pid: str, user: str, native: bool):
    def wrap_fn(fn, wants_user: bool):
        async def wrapped_fn(*args, **kwargs):
            if wants_user:
                return await fn(pid, user, *args, **kwargs)
            return await fn(pid, *args, **kwargs)
        return wrapped_fn

    tools = [t["tool_dict"] for t in entries]
    functions = {t["tool_dict"]["name"]: wrap_fn(t["function"], bool(t.get("wants_user")))
                 for t in entries}
    if native:
        tools = [{"type": "function", "function": t} for t in tools]
    return (tools if native else json.dumps(tools)), functions


def parse_json_toolcall(tool_call: str):
    tool_call = json.loads(tool_call)
    fn_name = tool_call.get("name")
    fn_args = tool_call.get("arguments", {})
    return fn_name, fn_args

def parse_pythonic_toolcall(tool_call: str):
    """
    """
    tool_call = ast.parse(tool_call[1:-1], mode='eval')
    fn_name = tool_call.body.func.id

    fn_args = {}
    for keyword in tool_call.body.keywords:
        fn_args[keyword.arg] = ast.literal_eval(keyword.value)
    return fn_name, fn_args

def normalize_native_toolcall(call: dict) -> tuple:
    fn = call.get("function") or {}
    name = fn.get("name") or ""
    args_raw = fn.get("arguments") or ""
    args = {}
    if isinstance(args_raw, str) and args_raw.strip():
        try:
            args = json.loads(args_raw)
        except (json.JSONDecodeError, TypeError):
            try:
                args = extract_json(args_raw)
            except ValueError:
                args = {}
    if not isinstance(args, dict):
        args = {}
    return name, args, call.get("id") or ""
