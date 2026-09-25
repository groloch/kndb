"""The agent's tools over note pages: reading what the project has written,
and writing into it as the calling user.

The reads are the agent's window on the notes — quizzes and summaries are
built on this text, and until now the agent could not see it. The writes
author every line as the caller (``wants_user``: ``prepare_tools`` binds the
calling user as second argument), so blame stays truthful; they are
``write`` level, so the guard prompts before any of them runs.
"""

import json

from ...data import notes, projects


def _page_row(p: dict) -> dict:
    """A page as a listing row: identity and authorship, never the content
    """
    return {"id": p["id"], "name": p["name"] or "(untitled)",
            "source_id": p["source_id"], "folder": p["folder"],
            "authors": p["authors"], "updated_at": p["updated_at"],
            "version": p["version"]}


async def _list_source_notes(pid: str, source_id: str):
    if not await projects.has_source(pid, source_id):
        return f"Source {source_id} is not in this project."
    pages = await notes.list_for_source(pid, source_id)
    if not pages:
        return "No note pages for this source."
    return json.dumps([_page_row(p) for p in pages])


async def _list_project_notes(pid: str, folder: str = ""):
    pages = await notes.list_standalone(pid, folder or None)
    return json.dumps([_page_row(p) for p in pages])


async def _read_note(pid: str, nid: str):
    page = await notes.get(nid)
    if page is None:
        return "Note not found. Note IDs start with 'nte_'."
    if page["project_id"] != pid:
        return f"Note {nid} is not in this project."
    head = f"# {page['name']}\nAuthors: {', '.join(page['authors']) or '?'}\n\n"
    content = page["content"] or ""
    if len(content) > 15000:
        content = content[:15000] + f"\n[… truncated, {len(content) - 15000} chars]"
    return head + content


async def _read_source_notes_combined(pid: str, source_id: str):
    if not await projects.has_source(pid, source_id):
        return f"Source {source_id} is not in this project."
    text = await notes.combined_text(pid, source_id)
    if not text:
        return "This source has no notes yet."
    if len(text) > 15000:
        text = text[:15000] + f"\n[… truncated, {len(text) - 15000} chars]"
    return text


async def _create_note(pid: str, user: str, name: str, source_id: str = "",
                       folder: str = "", content: str = ""):
    if source_id:
        if not await projects.has_source(pid, source_id):
            return f"Source {source_id} is not in this project."
        folder = ""          # a page attached to a source is never in a folder
    page = await notes.create(pid, author=user, source_id=source_id,
                              folder=folder, name=name, content=content)
    where = f"source {source_id}" if source_id else f"folder '{page['folder']}'"
    return json.dumps({"created": page["id"], "name": page["name"] or "(untitled)",
                       "in": where})


async def _append_note(pid: str, user: str, nid: str, text: str, header: str = ""):
    page = await notes.get(nid)
    if page is None:
        return "Note not found. Note IDs start with 'nte_'."
    if page["project_id"] != pid:
        return f"Note {nid} is not in this project."
    page = await notes.append(nid, text, author=user, header=header)
    return json.dumps({"appended_to": nid, "version": page["version"]})


async def _save_note(pid: str, user: str, nid: str, content: str):
    page = await notes.get(nid)
    if page is None:
        return "Note not found. Note IDs start with 'nte_'."
    if page["project_id"] != pid:
        return f"Note {nid} is not in this project."
    role = await projects.member_role(pid, user)
    page = await notes.save(nid, content, author=user, role=role or "")
    return json.dumps({"saved": nid, "version": page["version"]})


_list_source_notes_tool = {
    "security": "read",
    "function": _list_source_notes,
    "tool_dict": {
        "name": "list_source_notes",
        "description": "List the note pages attached to a source of this project. Returns a list of page rows (id, name, authors, updated_at), without their content.",
        "parameters": {
            "type": "object",
            "properties": {
                "source_id": {"type": "string"}
            },
            "required": ["source_id"]
        }
    }
}
_list_project_notes_tool = {
    "security": "read",
    "function": _list_project_notes,
    "tool_dict": {
        "name": "list_project_notes",
        "description": "List the standalone note pages of this project (the ones not attached to a source), optionally filtered by folder.",
        "parameters": {
            "type": "object",
            "properties": {
                "folder": {"type": "string", "description": "Optional folder path to filter by."}
            }
        }
    }
}
_read_note_tool = {
    "security": "read",
    "function": _read_note,
    "tool_dict": {
        "name": "read_note",
        "description": "Read the full content of one note page by its ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "nid": {"type": "string"}
            },
            "required": ["nid"]
        }
    }
}
_read_source_notes_combined_tool = {
    "security": "read",
    "function": _read_source_notes_combined,
    "tool_dict": {
        "name": "read_source_notes_combined",
        "description": "Read every note page of a source as one markdown document, the same text quizzes and summaries are built from.",
        "parameters": {
            "type": "object",
            "properties": {
                "source_id": {"type": "string"}
            },
            "required": ["source_id"]
        }
    }
}
_create_note_tool = {
    "security": "write",
    "wants_user": True,
    "function": _create_note,
    "tool_dict": {
        "name": "create_note",
        "description": "Create a note page written as the calling user: standalone in a folder, or attached to a source of this project.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "source_id": {"type": "string", "description": "Attach to this source; leave empty for a standalone page."},
                "folder": {"type": "string", "description": "Folder path for a standalone page; ignored when source_id is given."},
                "content": {"type": "string", "description": "Optional initial markdown content."}
            },
            "required": ["name"]
        }
    }
}
_append_note_tool = {
    "security": "write",
    "wants_user": True,
    "function": _append_note,
    "tool_dict": {
        "name": "append_note",
        "description": "Append text at the end of a note page, as the calling user. Never rewrites existing lines.",
        "parameters": {
            "type": "object",
            "properties": {
                "nid": {"type": "string"},
                "text": {"type": "string", "description": "Markdown text to append."},
                "header": {"type": "string", "description": "Optional small heading placed above the text."}
            },
            "required": ["nid", "text"]
        }
    }
}
_save_note_tool = {
    "security": "write",
    "wants_user": True,
    "function": _save_note,
    "tool_dict": {
        "name": "save_note",
        "description": "Replace the whole content of a note page, as the calling user. Lines the caller may not write (role rules) refuse the save. Prefer append_note unless a full rewrite is really needed.",
        "parameters": {
            "type": "object",
            "properties": {
                "nid": {"type": "string"},
                "content": {"type": "string", "description": "The complete new markdown content."}
            },
            "required": ["nid", "content"]
        }
    }
}


TOOLS = (
    _list_source_notes_tool,
    _list_project_notes_tool,
    _read_note_tool,
    _read_source_notes_combined_tool,
    _create_note_tool,
    _append_note_tool,
    _save_note_tool,
)
