import os
from pathlib import Path

import yaml


BASE_DIR = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = Path(os.environ.get("KNDB_CONFIG", BASE_DIR / "kndb.yaml"))

_DEFAULTS: dict = {
    "server": {"host": "127.0.0.1", "port": 5000, "debug": False},
    "data": {
        "dir": "data",
        "db_path": None,
    },
    "llm": {
        "base_url": "http://127.0.0.1:8000/v1",
        "api_key": "",
        "model": "LFM2.5-1.2B-Instruct",
        "retries": 3,
    },
    "permissions": {
        "default_user": "me",
        "roles": ["owner", "maintainer", "contributor", "spectator"],
        "default_role": "contributor",
        "owner_role": "owner",
        # Which roles each kind of action needs. Every name must appear in
        # ``roles``; the roles left out of a grant can only read.
        "grants": {
            "write": ["owner", "maintainer", "contributor"],
            "edit_others": ["owner", "maintainer"],
            "manage": ["owner", "maintainer"],
            "admin": ["owner"],
        },
    },
    "projects": {
        # Defaults a project is created with; each one is stored per project
        # and editable afterwards from the project settings panel.
        "personal_capabilities": {"allow_quiz": True, "multi_notes": False,
                                  "show_blame": True, "auto_import": True},
        "team_capabilities": {"allow_quiz": False, "multi_notes": True,
                              "show_blame": True, "auto_import": False},
        "member_colors": ["#3b82f6", "#ef4444", "#10b981", "#f59e0b",
                          "#8b5cf6", "#ec4899", "#14b8a6", "#f97316",
                          "#6366f1", "#84cc16", "#06b6d4", "#d946ef"],
        "external_color": "#94a3b8",
    },
    "limits": {
        "upload_mb": 300,
        "image_mb": 3,
        "image_downloads": 6,
        "min_html_chars": 500,
        "min_page_chars": 200,
        "web_markdown_chars": 12000,
        "web_markdown_max_tokens": 2500,
        "anchor_locator_chars": 4000,
        "anchor_doc_locator_chars": 16000,
    },
}

CAP_KEYS = ("allow_quiz", "multi_notes", "show_blame", "auto_import")


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            out[k] = _deep_merge(base[k], v)
        else:
            out[k] = v
    return out

def _load() -> dict:
    merged = _DEFAULTS
    if CONFIG_PATH.exists():
        try:
            raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise SystemExit(f"kndb config error in {CONFIG_PATH}: {e}") from e
        if not isinstance(raw, dict):
            raise SystemExit(
                f"kndb config error: {CONFIG_PATH} must contain a YAML mapping")
        merged = _deep_merge(_DEFAULTS, raw)
    return merged


_CONFIG = _load()


def _fail(msg: str):
    raise SystemExit(f"kndb config error in {CONFIG_PATH}: {msg}")

def _abs(path: str) -> str:
    p = Path(path)
    return str(p if p.is_absolute() else (BASE_DIR / p))

def _num(section: str, cast=int):
    """A number named by its dotted key, so a typo says which line to fix."""
    node = _CONFIG
    for part in section.split("."):
        node = node[part]
    try:
        return cast(node)
    except (TypeError, ValueError):
        _fail(f"{section} must be a number, got {node!r}")

def _strs(section: str, value) -> tuple:
    if not isinstance(value, (list, tuple)) or not all(
            isinstance(v, str) for v in value):
        _fail(f"{section} must be a list of strings")
    return tuple(value)

def _grant(name: str) -> tuple:
    """The roles allowed to perform one kind of action.

    A grant naming a role that does not exist is a silent lockout — the check
    would simply never pass — so it is a startup error instead."""
    roles = _strs(f"permissions.grants.{name}",
                  (_CONFIG["permissions"]["grants"] or {}).get(name))
    unknown = [r for r in roles if r not in ROLES]
    if unknown:
        _fail(f"permissions.grants.{name} names roles missing from "
              f"permissions.roles: {', '.join(unknown)}")
    return roles

def _caps(name: str) -> dict:
    """Per-project switches. Their names are database columns, so an unknown
    one is a typo that would otherwise be dropped without a word."""
    raw = _CONFIG["projects"][name] or {}
    unknown = set(raw) - set(CAP_KEYS)
    if unknown:
        _fail(f"projects.{name} has unknown capabilities: "
              f"{', '.join(sorted(unknown))}; choose from {', '.join(CAP_KEYS)}")
    return {k: bool(raw.get(k, _DEFAULTS["projects"][name][k])) for k in CAP_KEYS}


SERVER_HOST = str(_CONFIG["server"]["host"])
SERVER_PORT = _num("server.port")
DEBUG = bool(_CONFIG["server"]["debug"])

DATA_DIR = _abs(str(_CONFIG["data"]["dir"]))
_db_path = _CONFIG["data"]["db_path"] or os.path.join(
    str(_CONFIG["data"]["dir"]), "kndb.db")
DB_PATH = _abs(str(_db_path))

LLM_BASE_URL = str(_CONFIG["llm"]["base_url"]).rstrip("/")
LLM_API_KEY = str(_CONFIG["llm"]["api_key"])
LLM_MODEL = str(_CONFIG["llm"]["model"])
LLM_RETRIES = _num("llm.retries")

DEFAULT_USER = str(_CONFIG["permissions"]["default_user"]).strip() or "me"
ROLES = _strs("permissions.roles", _CONFIG["permissions"]["roles"])
DEFAULT_ROLE = str(_CONFIG["permissions"]["default_role"])
if DEFAULT_ROLE not in ROLES:
    _fail(f"permissions.default_role {DEFAULT_ROLE!r} is not in permissions.roles")
# The role a project creator gets, and the one a project may never run out of.
OWNER_ROLE = str(_CONFIG["permissions"]["owner_role"])
if OWNER_ROLE not in ROLES:
    _fail(f"permissions.owner_role {OWNER_ROLE!r} is not in permissions.roles")

WRITE_ROLES = _grant("write")            # add sources, write your own text
EDIT_OTHERS_ROLES = _grant("edit_others")  # rewrite text somebody else owns
MANAGE_ROLES = _grant("manage")          # members, settings, project contents
ADMIN_ROLES = _grant("admin")            # delete the project

PERSONAL_CAPS = _caps("personal_capabilities")
TEAM_CAPS = _caps("team_capabilities")
MEMBER_COLORS = _strs("projects.member_colors", _CONFIG["projects"]["member_colors"])
if not MEMBER_COLORS:
    _fail("projects.member_colors cannot be empty")
EXTERNAL_COLOR = str(_CONFIG["projects"]["external_color"])

UPLOAD_MAX_BYTES = int(_num("limits.upload_mb", float) * 1024 * 1024)
IMAGE_MAX_BYTES = int(_num("limits.image_mb", float) * 1024 * 1024)
IMAGE_DOWNLOADS = _num("limits.image_downloads")
MIN_HTML_CHARS = _num("limits.min_html_chars")
MIN_PAGE_CHARS = _num("limits.min_page_chars")
WEB_MD_CHARS = _num("limits.web_markdown_chars")
WEB_MD_MAX_TOKENS = _num("limits.web_markdown_max_tokens")
ANCHOR_LOC_CHARS = _num("limits.anchor_locator_chars")
ANCHOR_DOC_LOC_CHARS = _num("limits.anchor_doc_locator_chars")
