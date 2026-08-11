import os
from pathlib import Path

import yaml


BASE_DIR = Path(__file__).resolve().parent.parent
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
}


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


def _abs(path: str) -> str:
    p = Path(path)
    return str(p if p.is_absolute() else (BASE_DIR / p))


SERVER_HOST = str(_CONFIG["server"]["host"])
SERVER_PORT = int(_CONFIG["server"]["port"])
DEBUG = bool(_CONFIG["server"]["debug"])

DATA_DIR = _abs(str(_CONFIG["data"]["dir"]))
_db_path = _CONFIG["data"]["db_path"] or os.path.join(
    str(_CONFIG["data"]["dir"]), "kndb.db")
DB_PATH = _abs(str(_db_path))

LLM_BASE_URL = str(_CONFIG["llm"]["base_url"]).rstrip("/")
LLM_API_KEY = str(_CONFIG["llm"]["api_key"])
LLM_MODEL = str(_CONFIG["llm"]["model"])
LLM_RETRIES = int(_CONFIG["llm"]["retries"])
