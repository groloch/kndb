"""The agent's tools over the source library: the reads that let it find
and read the project's documents. All ``read`` level — auto-allowed, the
guard never prompts for them.
"""

import json

from ...data import notes, projects, store


async def _list_sources(pid: str):
    sources = await store.list_sources(pid=pid)

    sources = {s["id"]: {"title": s["title"]} for s in sources}
    if len(sources) == 0:
        return "No sources found."
    return json.dumps(sources)

async def _list_sources_tags(pid: str):
    sources = await store.list_sources(pid=pid)

    tags = {t.strip() for s in sources for t in (s["tags"] or "").split(",")
            if t.strip()}

    if len(tags) == 0:
        return "No tags found."
    return json.dumps(sorted(tags))

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
    # local import: services imports this package, so a module-level one
    # would be circular
    from ..services import document_text

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


async def _get_source_meta(pid: str, source_id: str):
    rows = await store.list_sources(pid=pid)
    for r in rows:
        if r["id"] == source_id:
            return json.dumps(r)
    return (f"Source {source_id} is not in this project. "
            f"Call list_sources for the IDs.")


async def _list_folder_tree(pid: str):
    """The workspace's tree in miniature: every folder path, with the sources
    filed into each — enough for the agent to navigate without the full tree
    """
    folders = await projects.list_folders(pid)
    links = await projects.source_links(pid)
    by_folder = {}
    for link in links:
        by_folder.setdefault(link["folder"], []).append(link["source_id"])
    return json.dumps({"folders": folders,
                       "sources_per_folder": {f or "(root)": ids
                                              for f, ids in sorted(by_folder.items())}})


async def _get_folder_readme(pid: str, folder: str):
    page = await notes.readme_for_folder(pid, _unquote(folder))
    if page is None:
        return f"No readme for folder '{_unquote(folder) or '(root)'}'."
    return page["content"] or "(empty readme)"


def _unquote(folder: str) -> str:
    """A folder argument with the stray quotes or whitespace a model may wrap
    it in stripped — the tree's own names never carry quotes
    """
    return (folder or "").strip().strip("'\"").strip()


async def _add_tag_to_source(pid: str, source_id: str, tag: str):
    source = await store.get_source(source_id)
    if source is None:
        return "Source not found."
    if not await projects.has_source(pid, source_id):
        return f"Source {source_id} is not in this project."
    tag = (tag or "").strip()
    if not tag:
        return "tag required."
    tags = [t.strip() for t in (source["tags"] or "").split(",") if t.strip()]
    if tag not in tags:
        tags.append(tag)
        await store.update_meta(source_id, tags=", ".join(tags))
    return json.dumps({"source_id": source_id, "tags": tags})


async def _create_folder(pid: str, path: str):
    try:
        created = await projects.create_folder(pid, _unquote(path))
    except ValueError as e:
        return f"could not create folder: {e}"
    return json.dumps({"created": created})


async def _move_source(pid: str, source_id: str, folder: str):
    if not await projects.has_source(pid, source_id):
        return f"Source {source_id} is not in this project."
    moved = await projects.set_source_folder(pid, source_id, _unquote(folder))
    if not moved:
        return f"Source {source_id} is not in this project."
    return json.dumps({"source_id": source_id,
                       "folder": projects.norm_folder(folder)})


async def _add_source_to_project(pid: str, source_id: str, folder: str = ""):
    source = await store.get_source(source_id)
    if source is None:
        return "Source not found. It may have to be imported first — the agent cannot import."
    added = await projects.add_source(pid, source_id, folder=_unquote(folder))
    if not added:
        return f"Source {source_id} is already in this project."
    return json.dumps({"added": source_id, "title": source["title"],
                       "folder": projects.norm_folder(folder)})


async def _remove_source_from_project(pid: str, source_id: str, confirm: bool = False):
    """Unlinks the source from this project. Destructive beyond the link:
    the unlinking takes the project's quiz, the source's note pages and their
    anchors with it, so it asks for ``confirm`` on top of the guard prompt
    """
    if not confirm:
        return ("remove_source_from_project takes this source's notes, quiz and "
                "anchors of this project with it. Call it again with confirm=true "
                "to really remove it.")
    removed = await projects.remove_source(pid, source_id)
    if not removed:
        return f"Source {source_id} is not in this project."
    return (f"Source {source_id} removed from the project. It stays in the "
            f"library and in any other project holding it.")


async def _rename_folder(pid: str, path: str, new_path: str):
    try:
        renamed = await projects.rename_folder(pid, _unquote(path), _unquote(new_path))
    except ValueError as e:
        return f"could not rename folder: {e}"
    return json.dumps({"renamed_to": renamed})


async def _delete_folder(pid: str, path: str, confirm: bool = False):
    """Only the folder rows go: what the folder held is lifted into its
    parent, never deleted. Still gated behind ``confirm`` — reorganising a
    tree from a chat is easy to get wrong
    """
    if not confirm:
        return ("delete_folder lifts everything it held into the parent "
                "folder. Call it again with confirm=true to really delete "
                f"'{path}'.")
    try:
        await projects.delete_folder(pid, path)
    except ValueError as e:
        return f"could not delete folder: {e}"
    return json.dumps({"deleted": path})


_list_sources_tool = {
    "security": "read",
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
    "security": "read",
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
    "security": "read",
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
    "security": "read",
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
    "security": "read",
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
    "security": "read",
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
_get_source_meta_tool = {
    "security": "read",
    "function": _get_source_meta,
    "tool_dict": {
        "name": "get_source_meta",
        "description": "Get the metadata of a source of this project: title, type, URL, tags, fetch date, and how many quiz questions it has here.",
        "parameters": {
            "type": "object",
            "properties": {
                "source_id": {"type": "string"}
            },
            "required": ["source_id"]
        }
    }
}
_list_folder_tree_tool = {
    "security": "read",
    "function": _list_folder_tree,
    "tool_dict": {
        "name": "list_folder_tree",
        "description": "List every folder of this project and which sources are filed into each, to navigate the workspace.",
        "parameters": {
            "type": "object",
            "properties": {

            }
        }
    }
}
_get_folder_readme_tool = {
    "security": "read",
    "function": _get_folder_readme,
    "tool_dict": {
        "name": "get_folder_readme",
        "description": "Read the readme page of a folder — its description and conventions. The root folder is the empty string.",
        "parameters": {
            "type": "object",
            "properties": {
                "folder": {"type": "string", "description": "Folder path; empty for the root."}
            },
            "required": ["folder"]
        }
    }
}
_add_tag_to_source_tool = {
    "security": "write",
    "function": _add_tag_to_source,
    "tool_dict": {
        "name": "add_tag_to_source",
        "description": "Add one tag to a source of this project (keeping its existing tags).",
        "parameters": {
            "type": "object",
            "properties": {
                "source_id": {"type": "string"},
                "tag": {"type": "string"}
            },
            "required": ["source_id", "tag"]
        }
    }
}
_create_folder_tool = {
    "security": "write",
    "function": _create_folder,
    "tool_dict": {
        "name": "create_folder",
        "description": "Create a folder in this project (ancestors are created as needed).",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Folder path, e.g. 'papers/2024'."}
            },
            "required": ["path"]
        }
    }
}
_move_source_tool = {
    "security": "write",
    "function": _move_source,
    "tool_dict": {
        "name": "move_source",
        "description": "File a source of this project under a folder, creating the folder if needed. An empty folder moves it back to the root.",
        "parameters": {
            "type": "object",
            "properties": {
                "source_id": {"type": "string"},
                "folder": {"type": "string", "description": "Target folder path; empty for the root."}
            },
            "required": ["source_id", "folder"]
        }
    }
}
_add_source_to_project_tool = {
    "security": "write",
    "function": _add_source_to_project,
    "tool_dict": {
        "name": "add_source_to_project",
        "description": "Link a source that exists in the library into this project, optionally under a folder. The source must already be imported.",
        "parameters": {
            "type": "object",
            "properties": {
                "source_id": {"type": "string"},
                "folder": {"type": "string", "description": "Folder path to file it under; empty for the root."}
            },
            "required": ["source_id"]
        }
    }
}
_remove_source_from_project_tool = {
    "security": "admin",
    "function": _remove_source_from_project,
    "tool_dict": {
        "name": "remove_source_from_project",
        "description": "Unlink a source from this project. Its note pages, quiz and anchors of this project are deleted with the link; the source itself stays in the library. Requires confirm=true.",
        "parameters": {
            "type": "object",
            "properties": {
                "source_id": {"type": "string"},
                "confirm": {"type": "boolean", "description": "Must be true to actually remove."}
            },
            "required": ["source_id", "confirm"]
        }
    }
}
_rename_folder_tool = {
    "security": "write",
    "function": _rename_folder,
    "tool_dict": {
        "name": "rename_folder",
        "description": "Move or rename a folder subtree; the sources and note pages inside move with it.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "new_path": {"type": "string"}
            },
            "required": ["path", "new_path"]
        }
    }
}
_delete_folder_tool = {
    "security": "admin",
    "function": _delete_folder,
    "tool_dict": {
        "name": "delete_folder",
        "description": "Delete a folder: the folder rows only — everything it held is lifted into its parent, nothing is deleted. Requires confirm=true.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "confirm": {"type": "boolean", "description": "Must be true to actually delete."}
            },
            "required": ["path", "confirm"]
        }
    }
}


TOOLS = (
    _list_sources_tool,
    _list_sources_tags_tool,
    _search_sources_by_title_tool,
    _search_sources_by_tags_tool,
    _get_source_content_tool,
    _get_source_tags_tool,
    _get_source_meta_tool,
    _list_folder_tree_tool,
    _get_folder_readme_tool,
    _add_tag_to_source_tool,
    _create_folder_tool,
    _move_source_tool,
    _add_source_to_project_tool,
    _remove_source_from_project_tool,
    _rename_folder_tool,
    _delete_folder_tool,
)
