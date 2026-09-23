# KNDB — Project Map for Agents

This document is the orientation file for anyone (human or AI agent) working in
this repository. It describes **what KNDB is**, **how it is structured**, the
**data model**, the **request flow**, and the **conventions** you must respect
when modifying code.

Read this file before touching anything. Each module listed below also carries a
docstring at the top that says what it does — trust the code, not just this map.

---

## 1. What KNDB is

**KNDB (Knowledge DataBase)** is a self-hosted, all-in-one *personal knowledge
database* delivered as a web application.

It lets a person:

- **Import** scientific papers (arXiv), Hugging Face model cards, arbitrary web
  pages (archived with styles and images), and local `.pdf` / `.md` files.
- **Annotate** every imported document with personal **markdown notes**.
- **Ground notes in the source** with **anchors** (a sentence in a note tied to
  a passage in the document, with the PDF viewer under its own control).
- **Generate quizzes** (multiple choice) from a source and/or the user's notes,
  and **play** them via **spaced repetition** for efficient memorization.
- **Summarize** any source using the LLM and append the result to the notes.
- **Organize** work into **projects** shared with other people, with
  **role-based permissions** and **line-level blame**.
- **Ask questions** about the documents through an in-project **agent** that
  calls search/read tools over the library.

**Key architectural fact:** KNDB does **not** run a model. It is a client to an
**OpenAI-compatible server** (vLLM, llama.cpp, Ollama, OpenAI, LiteLLM…). All
configuration points at that server via `kndb.yaml`.

**Stack:** Python 3.12 + FastAPI (ASGI, run under uvicorn) + SQLAlchemy (async,
aiosqlite) on the backend; plain vanilla JavaScript + hand-written HTML/CSS with
vendored third-party JS (no build step, no Node, no frontend framework) on the
frontend.

---

## 2. Top-level layout

```
kndb/
├── app.py                 # The ASGI application: lifespan, routers, error shape, static mount
├── kndb.yaml              # All configuration (server, data, llm, permissions, projects, limits)
├── README.md              # Human-facing overview & quick start
├── requirements.txt       # Python dependencies
├── .gitignore
├── backend/               # Python backend (FastAPI app, business + data logic)
├── static/                # Frontend: HTML shells, JS, CSS, vendored JS libs
├── prompts/               # LLM prompt templates (Markdown, Jinja). 
├── data/                  # Runtime data: SQLite DB + source documents (git-ignored)
├── dev_tools/             # One-off developer scripts (vendor fetcher)
└── .kndb_venv/            # Local virtualenv (git-ignored)
```

> `app.py` is the entrypoint. `python app.py` starts `uvicorn.run("app:app", ...)`.
> All routers are registered there, so that is the first place to look when you
> wonder whether a route exists.

---

## 3. Backend — `backend/`

The backend is organised in four layers. Imports flow **downward** only:
`api` → `content` → `data` → `core` and `integrations`. Nothing layer should
import upward.

```
backend/
├── __init__.py
├── api/            # FastAPI routers: HTTP surface, auth/role lookups, SSE
├── content/        # Business logic that uses the LLM and acts on the library
├── integrations/   # The only code that knows about the model endpoint (llm.py)
├── data/           # Persistence: SQLAlchemy queries + on-disk source blobs
└── core/           # Wiring: config, DB engine, schema, migrations
```

### 3.1 `backend/core/` — foundation

| File | Purpose |
|------|---------|
| `config.py` | Every setting, resolved once at import. Reads `kndb.yaml` (or `$KNDB_CONFIG`) and deep-merges over a built-in `_DEFAULTS` dict. Malformed config refuses to start. Import the resolved values directly (e.g. `config.SERVER_PORT`). |
| `db.py` | The SQLAlchemy ORM models — **the schema of record** — plus the async engine and session factory. Nothing opens at import; the engine is created on first use, the file by `init_db()`. |
| `migrations.py` | Runs on every boot and brings an older DB file up to the current models. Every step must be idempotent (no-op the second time), either by asking SQLite what's there or by flagging completion in the `schema_meta` table. |

### 3.2 `backend/data/` — persistence

Each module owns a coherent slice of the data. All are **async** and map to the
models defined in `core/db.py`.

| File | Purpose |
|------|---------|
| `store.py` | **The source library**: one metadata row per source plus its document blob on disk. Rows and files are written separately (not atomic). |
| `users.py` | User accounts. The user **name is the identity** string that the rest of the backend keys on (note authorship, blame, project membership). |
| `projects.py` | Projects: members, sources linked into a project, and the folder tree holding sources and note pages. Each call opens and commits its own session (cascades are not atomic). |
| `notes.py` | Note pages: per-author documents hanging off a source or standalone in a folder. Every line carries its author in a run-length **blame map**. |
| `anchors.py` | Anchors: a note sentence tied to a source passage. An anchor is its own row (the note text is never marked up). |
| `quiz.py` | Quiz contents and play stats, stored as JSON blobs keyed by `(project, source)`. |

### 3.3 `backend/integrations/` — external systems

| File | Purpose |
|------|---------|
| `llm.py` | The **only** code that knows about the model endpoint. One shared httpx client, the retry policy, and the prompt loader. Failures surface as `LLMError` whose message is meant to be shown to the user as-is. |

### 3.4 `backend/content/` — business logic on top of the library

| File | Purpose |
|------|---------|
| `services.py` | What the app does with a stored source: quizzes, summaries, spaced repetition, and the agent. Reads note pages + document text, streams the model's answer, writes results through the stores. Also builds `document_text(...)` (the clean text used for LLM calls). |
| `fetchers.py` | Fetching a URL into a source blob: arXiv, Hugging Face, and plain web (archive with inlined CSS, embedded images, clean markdown). Raises on failure (routes turn that into a 502). One shared httpx client. |
| `agent.py` | The in-project question-answering agent's **tool definitions** and implementations (`list_sources`, `search_sources_*`, `get_source_content`, `get_source_tags`) plus tool-call parsers for both pythonic and JSON call syntax. |
| `sessions.py` | The agent's **per-session memory**, in process memory only: one conversation per `(project, user)`, holding the message history the next run continues and the display log a reloaded page replays. Capped by turns/characters, TTL-pruned, one run at a time per session. Restart = fresh; the DB-backed version of this is ideas.md §3a. |
| `blame.py` | Pure line-authorship computation (run-length encoded) and note-diffing helpers. No I/O, no clock — the caller supplies timestamps. |

### 3.5 `backend/api/` — HTTP surface (FastAPI routers)

All routers are imported and mounted in `app.py`. They depend on helpers in
`deps.py` for lookups, identity, and role checks.

| File | Purpose / routes |
|------|------------------|
| `deps.py` | Shared router dependencies: lookups that 404 (`get_source_or_404`, `get_note_or_404`), who the caller is (`current_user`), role checks (`require_role` / the `WRITE`…) and the **SSE** plumbing (`sse`, `sse_headers`). |
| `sources_routes.py` | Getting documents in and serving them back out. A source is **global** (one blob, one row, however many projects link to it) so re-importing a URL dedupes. |
| `projects_routes.py` | Projects, members, and contents. Read → membership; change → manage role; delete → admin role. |
| `notes_routes.py` | Note pages (attached or standalone). Answer to project roles; content is owned **line by line** (writing over others' lines needs a maintainer role). |
| `anchors_routes.py` | Create/list/delete anchors. An anchor is an annotation, never an edit. |
| `learning_routes.py` | **Quizzes and summaries** — the two things the model is asked to write. Quizzes are private to the caller's workspace. |
| `agent_routes.py` | `POST /api/agent/message/{pid}` — streams the agent's answer via SSE. `GET`/`DELETE /api/agent/session/{pid}` — replay/reset the caller's per-session conversation. |
| `transfer_routes.py` | Moving work between projects (source, page, or a few lines) — origin keeps its copy; text keeps blame + a provenance header. |
| `llm_routes.py` | `GET /api/llm/status` — whether a model is reachable, display name, last error (re-probes on unknown/failure). |
| `pages_routes.py` | The **HTML shells** (`/`, `/projects`, `/projects/{pid}`). These are static page loads; all content is fetched via the JSON API. Never checks membership at page level. |

---

## 4. Frontend — `static/`

Vanilla JS, no framework, no build step. Shared plumbing is in `common.js`,
which **must load first** on every page. The personal workspace and a project's
Library tab are **the same page**, drawn by `library.js`.

```
static/
├── index.html         # Workspace page shell
├── projects.html      # Project list page shell
├── project.html       # Project workspace shell
├── app.js             # Workspace-only bits: importing + the quiz UI
├── projects.js        # Project list page logic
├── project.js         # Project-page-only additions (multi-note, members, settings)
├── library.js         # The shared Library page: tree | document | notes
├── tree.js            # The library tree (folders, sources, standalone notes)
├── surface.js         # *Surface* abstraction over document/note HTML preview
├── anchors.js         # Anchoring UI (note→source and source→note)
├── pdfview.js         # Own PDF renderer (so selections can be read out)
├── agent.js           # Agent chat UI
├── style.css
├── templates/         # Jinja2 partials (kndb_header, notes_editor, source_browser, source_viewer)
└── vendor/            # Third-party JS (git-ignored; install via dev_tools/fetch_vendor.py)
```

- API calls, identity header, markdown rendering, toasts, SSE streaming, and
  modals live in `common.js`.
- Backend responses are uniformly `{ok: true, ...}` / `{ok: false, error: ...}`
  (enforced by the exception handlers in `app.py`).

---

## 5. Prompts — `prompts/`

Markdown/Jinja templates sent to the LLM. The `services.py` loader renders them.

| File | Purpose |
|------|---------|
| `project_agent.md` | Role + system prompt for the in-project question answering agent (includes the project's description and the dynamically-listed tools). |
| `quiz_creation.md` | Build a multiple-choice quiz strictly from provided notes/document, returning only a JSON array matching a fixed schema. |
| `summarization.md` | Faithful, structured markdown summary of a source (overview, method, results with tables, limitations, conclusion…). |
| `web_to_markdown.md` | Convert raw web content into clean structured Markdown for offline archival (remove nav/cookies/ads, preserve content). |

---

## 6. Data model & storage

Everything except the document blobs lives in **one SQLite database**
(`data/kndb.db`, git-ignored). Source documents stay on disk under `data/sources/`:

```
data/
  kndb.db                 # sources, projects, notes, anchors, quizzes, users
  sources/<id>.<ext>      # the fetched/uploaded document (pdf / md / html)
  sources/<id>.txt        # optional clean-text rendition, used for LLM calls
```

Source IDs follow the `src_<hex>` pattern. Note pages are authored line-by-line
with a run-length **blame map**; anchors are standalone rows so note text is
never modified by linking. Quizzes are JSON blobs keyed by `(project, source)`
and are private per user workspace.

> `.gitignore` ignores `data/*.db`, `sources/`, `static/vendor/`, `.venv/`,
> `__pycache__/`. Runtime data is not version-controlled.

---

## 7. Configuration — `kndb.yaml`

Loaded once at import by `core/config.py`. Omitted keys fall back to the
`_DEFAULTS` dict in that module. Key sections:

- `server` — host, port, debug (auto-reload).
- `data` — data directory (relative to project root or absolute).
- `llm` — `base_url`, `api_key`, `model`, `retries`. Points at any
  OpenAI-compatible server.
- `permissions` — default user/role, the ordered role list
  (`owner > maintainer > contributor > spectator`), and which roles may
  `write`, `edit_others`, `manage`, `admin`.
- `projects` — default capabilities for personal vs. team projects, member
  colors, external-author color.
- `limits` — upload/image size caps, concurrency, and the various
  character/token limits governing LLM calls and anchor locators.

---

## 8. Request flow (end to end)

1. Browser loads an HTML shell from `pages_routes.py`.
2. The page then calls JSON API endpoints (`/api/...`) defined in `backend/api/*_routes.py`.
3. Each API call goes through `backend/api/deps.py`: identity (from the
   identity header, dev-mode), existence lookups, and role checks.
4. Route handlers call business logic in `backend/content/` which:
   - talks to the model server only through `backend/integrations/llm.py`, and
   - reads/writes data only through `backend/data/*`.
5. Streaming responses (quizzes, summaries, agent messages) use **SSE**
   (`sse` / `sse_headers` in `deps.py`), consumed by `common.js`'s `streamSSE`.
6. Every response (including errors) follows the `{ok, error}` shape.

---

## 9. Conventions & rules for contributors

- **Layer boundaries:** `api` → `content` → `data` → `core`/`integrations`.
  Never import upward; the model-endpoint details stay inside `integrations/llm.py`.
- **Schema of record:** `core/db.py` leads; `core/migrations.py` only catches an
  older file up to it. If you change the ORM models, add an idempotent migration.
- **Idempotent boot migrations:** every migration step must be a safe no-op on
  re-run.
- **Identity:** the *user name string* is the identity the backend keys on.
  When auth is implemented later, it will land here.
- **Error shape:** raise `HTTPException` for expected failures; the handlers in
  `app.py` convert everything (including unhandled exceptions) to
  `{ok: false, error: ...}`.
- **Frontend:** never reference a CDN at runtime. Third-party JS is vendored
  under `static/vendor/` via `dev_tools/fetch_vendor.py` (pinned SHA-256s, plain
  stdlib, offline after install). If you add a frontend dependency, add it there.
- **`common.js` loads first** on every page; shared helpers live there.
- **Cache header discipline:** HTML shells and static assets are served with
  `Cache-Control: no-cache` (revalidated, not fingerprinted) — keep that intact
  so reloads never serve stale markup/scripts.
- **Runtime data is local:** `data/` and `static/vendor/` are git-ignored; never
  commit generated/runtime artifacts.
- **New prompts** that the LLM needs go in `prompts/` and are loaded through the
  prompt loader in `integrations/llm.py` (via `services.py`).
- Every module carries a purpose-stating docstring — keep them accurate.

---

## 10. Dev workflow cheat sheet

```bash
# setup (once)
python -m venv .venv && .venv/bin/pip install -r requirements.txt
python dev_tools/fetch_vendor.py            # pull vendored frontend JS

# run a local LLM server (any OpenAI-compatible server)
vllm serve <model> --port 1234   # match kndb.yaml llm.base_url

# run the app
.venv/bin/python app.py           # serves http://127.0.0.1:5000 by default
```

---

## 11. Where to start depending on your task

| If you are working on… | Start here |
|------------------------|------------|
| Adding an API endpoint | `backend/api/` (pick the right router) + register in `app.py` |
| A new data entity / field | `backend/core/db.py` (model) + `backend/core/migrations.py` + `backend/data/` |
| LLM features (quiz/summary/agent) | `backend/content/services.py` + `prompts/` |
| A new fetch/import kind | `backend/content/fetchers.py` |
| Frontend behaviour | `static/common.js`, then the relevant `static/*.js` page |
| Permissions / roles | `kndb.yaml` + `backend/api/deps.py` |
| Configuration | `kndb.yaml` + `backend/core/config.py` |
