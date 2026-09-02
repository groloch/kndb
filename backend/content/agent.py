import ast
import json

from backend.data import store

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
    content = await document_text(source) if source else ""
    if content == "":
        return "Source has no content."
    return content[:15000]

async def _get_source_tags(pid: str, source_id: str):
    source = await store.get_source(source_id)
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


def prepare_tools(pid: str):
    tool_list = [
        _list_sources_tool,
        _list_sources_tags_tool,
        _search_sources_by_title_tool,
        _search_sources_by_tags_tool,
        _get_source_content_tool,
        _get_source_tags_tool
    ]

    def wrap_fn(fn):
        async def wrapped_fn(*args, **kwargs):
            return await fn(pid, *args, **kwargs)
        return wrapped_fn

    tools = json.dumps([
        tool["tool_dict"]
        for tool in tool_list
    ])
    functions = {
        tool["tool_dict"]["name"]: wrap_fn(tool["function"])
        for tool in tool_list
    }
    return tools, functions

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