import ast
import json

from backend.data import projects, store

# list_sources, list_sources_tags
# search_sources_by_title, search_sources_by_tags
# get_source_content, get_source_tags


async def _list_sources(pid: str):
    sources = await store.list_sources(pid=pid)

    sources = {s["id"]: {"title": s["title"]} for s in sources}
    if len(sources) == 0:
        return "No sources found."
    return json.dumps(sources)

async def _list_sources_tags(pid: str):
    sources = await store.list_sources(pid=pid)

    tags = {s["tags"] for s in sources if s["tags"]}

    if len(tags) == 0:
        return "No tags found."
    return json.dumps(list(tags))

async def _search_sources_by_title(pid: str, title: str):
    sources = await store.list_sources(pid=pid, q=title)
    sources = {s["id"]: s["title"] for s in sources}

    if len(sources) == 0:
        return "No matching sources found (wrong title?)."
    return json.dumps(sources)

async def _search_sources_by_tags(pid: str, tags: list):
    sources = await store.list_sources(pid=pid)
    sources = {s["id"]: s["title"] for s in sources if s["tags"] and any(tag in s["tags"] for tag in tags)}
    if len(sources) == 0:
        return "No matching sources found."
    return json.dumps(sources)

async def _get_source_content(pid: str, source_id: str):
    from backend.content.services import document_text

    source = await store.get_source(source_id)
    if source is None:
        error_msg = "Source not found."
        if not source_id.startswith("src_"):
            error_msg += " Wrong source ID format: it should start with 'src_'."
        return error_msg
    if not await projects.has_source(pid, source_id):
        return f"Source {source_id} is not in this project."
    content = await document_text(source)
    if content == "":
        return "Source has no content."
    return content[:15000]

async def _get_source_tags(pid: str, source_id: str):
    source = await store.get_source(source_id)
    if source is None:
        return "Source not found."
    if not await projects.has_source(pid, source_id):
        return f"Source {source_id} is not in this project."
    return json.dumps(source["tags"])


_list_sources_tool = {
    "function": _list_sources,
    "tool_dict": {
        "name": "list_sources",
        "description": "List all sources inside the project. Returns a dictionary with source IDs as keys and titles as values.",
        "parameters": {
            "type": "object",
            "properties": {

            }
        }
    }
}
_list_sources_tags_tool = {
    "function": _list_sources_tags,
    "tool_dict": {
        "name": "list_sources_tags",
        "description": "List all tags of sources inside the project.",
        "parameters": {
            "type": "object",
            "properties": {

            }
        }
    }
}
_search_sources_by_title_tool = {
    "function": _search_sources_by_title,
    "tool_dict": {
        "name": "search_sources_by_title",
        "description": "Search sources whose title contains the given string. Returns a dictionary with matched source IDs as keys and titles as values.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string"
                }
            },
            "required": ["title"]
        }
    }
}
_search_sources_by_tags_tool = {
    "function": _search_sources_by_tags,
    "tool_dict": {
        "name": "search_sources_by_tags",
        "description": "Search sources whose tags contain the given tags. Returns a dictionary with matched source IDs as keys and titles as values.",
        "parameters": {
            "type": "object",
            "properties": {
                "tags": {
                    "type": "array",
                    "items": {
                        "type": "string"
                    }
                }
            },
            "required": ["tags"]
        }
    }
}
_get_source_content_tool = {
    "function": _get_source_content,
    "tool_dict": {
        "name": "get_source_content",
        "description": "Get the text content of a source by its ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "source_id": {
                    "type": "string"
                }
            },
            "required": ["source_id"]
        }
    }
}
_get_source_tags_tool = {
    "function": _get_source_tags,
    "tool_dict": {
        "name": "get_source_tags",
        "description": "Get the tags of a source by its ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "source_id": {
                    "type": "string"
                }
            },
            "required": ["source_id"]
        }
    }
}


_TOOLS = (
    _list_sources_tool,
    _list_sources_tags_tool,
    _search_sources_by_title_tool,
    _search_sources_by_tags_tool,
    _get_source_content_tool,
    _get_source_tags_tool,
)


def tool_dicts() -> list:
    """The six tool schemas as JSON-Schema-ish dicts, in prompt order.
    The single source of truth: prepare_tools renders these, so a tool cannot
    exist in the prompt without a function to run it, or be described
    otherwise
    """
    return [t["tool_dict"] for t in _TOOLS]


def prepare_tools(pid: str, native: bool = False):
    """(tools, functions) for one agent run.
    native=True → tools is the schema list sent to the server in the request
    (it knows how to format the call); native=False → the same schemas as the
    JSON text the prompt shows the model, which writes pythonic calls back.
    functions maps each tool name to its pid-bound async call in both cases
    """

    def wrap_fn(fn):
        async def wrapped_fn(*args, **kwargs):
            return await fn(pid, *args, **kwargs)
        return wrapped_fn

    tools = tool_dicts()
    functions = {t["tool_dict"]["name"]: wrap_fn(t["function"]) for t in _TOOLS}
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
    """(name, args, call_id) out of a server-side tool_calls record.
    arguments arrives as an unparsed JSON string; it is read tolerantly, so a
    server that truncates or mangles it still moves the loop along instead of
    killing the run
    """
    from backend.integrations.llm import extract_json

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