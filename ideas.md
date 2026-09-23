# KNDB — Ideas & Directions

A catalogue of ideas for where KNDB could go now that the original scope is
implemented. Nothing here is planned — each idea stands on its own, and each
one sketches *what* it is, *why* it fits KNDB, and roughly *how* it could be
built within the existing architecture (`api` → `content` → `data` →
`core`/`integrations`, schema of record in `core/db.py`, prompts in `prompts/`,
vanilla-JS frontend, everything configured via `kndb.yaml`).

---

## 1. Real authentication

**Status today.** Identity is claimed, never proved. `current_user` in
`backend/api/deps.py` reads an `X-KNDB-User` header or a `kndb_user` cookie,
and registers the name on first sight. Roles, blame, project permissions and
the agent's read-only gating all rest on this honor system. The project docs
themselves say auth "will land here" (`deps.py`).

**The idea.** Make the claimed identity a proved one.

- **Password login + signed session cookie.** A `users` table already exists;
  add a password hash column (argon2 or bcrypt), a `POST /api/auth/login`
  route, and have `current_user` verify a signed cookie (itsdangerous /
  `hashlib`-based HMAC — no new heavy dependency needed) instead of trusting
  the header. The header keeps working in dev mode behind a config flag
  (`kndb.yaml: permissions.dev_identity_header`), so tests and local scripts
  keep working.
- **Single-user passphrase gate.** If multi-user isn't actually the goal,
  a single shared passphrase that unlocks a session is enough, and is an
  afternoon of work.
- **OIDC pass-through** if KNDB ever gets hosted behind a reverse proxy with
  an identity provider. `current_user` is the single chokepoint — one function
  to change, every router inherits the change.

**Why it matters.** It is the prerequisite for running KNDB anywhere except
`127.0.0.1` — on a home server, a VPS, or shared with a team. Every other
multi-user feature (roles, blame, shared projects, kanban assignees) silently
assumes it. It is also the gate for the agent becoming write-capable (§3):
you can only let the agent write on behalf of a *proved* identity.

**Sketch.**

- `core/config.py`: add auth section to `_DEFAULTS`
  (`method: none|password|oidc`, session TTL, dev-header flag).
- `core/db.py` + `migrations.py`: password hash + created_at on `User`.
- `api/`: `auth_routes.py` (`/api/auth/login`, `/api/auth/logout`,
  `/api/auth/me`); `deps.current_user` verifies the signed cookie, falls back
  to the header only when `method: none`.
- Frontend: a login shell page or a modal when a request returns 401;
  `common.js` already centralizes API calls, so retry-after-login is one place.

---

## 2. Search depth: full-text and semantic

**Status today.** Search is title-substring + `@tag` + `/project`
(`library.js` builds the query, `projects_routes.py` filters in Python with
substring matching, the agent's `search_sources_by_title` goes through the
same path). Meanwhile the app stores the **full text** of every source (the
`.txt` sidecar used for LLM calls), all note pages, and all tags — and none of
it is searchable.

**The idea.** Two stages, both self-contained.

### 2a. SQLite FTS5 full-text search

Use SQLite's built-in FTS5 virtual tables — zero new infrastructure, no
daemon, no new dependency (bundled with Python's sqlite3). Index:

- the clean-text rendition of each source (`<id>.txt`, or extract from the
  blob), plus title and tags
- every note page's content
- quiz questions, if wanted

New routes like `GET /api/search?q=...&project=...` return matches with
snippets (`snippet()` function of FTS5) and the FTS5 rank. The library tree
and the `/learning` page get a real search box; searching a phrase finds the
PDF whose body contains it.

It also upgrades the **agent for free**: `search_sources_by_title` becomes a
full-text `search_sources` tool, which is a strict improvement for the "find
the paper that mentioned X" class of questions the agent is meant for.

**Sketch.**

- `core/db.py`: FTS5 virtual table + triggers from `sources`/`note_pages`, or
  (simpler, since content lives partly on disk) a dedicated `search_index`
  table maintained by `data/store.py` and `data/notes.py` on write.
- `core/migrations.py`: idempotent rebuild step (drop & re-create, populate
  from the `.txt` sidecars at first boot).
- `data/search.py`: `index_source`, `index_note`, `query(q, pid=None)` →
  rows with `sid`, `snippet`, `rank`.
- `api/search_routes.py`; wire the agent tool through `content/agent.py`.

### 2b. Semantic search via embeddings

Consistent with KNDB's "bring your any OpenAI-compatible server" philosophy:
every OpenAI-compatible server exposes `/v1/embeddings`, so no model runs
inside KNDB here either. Embed each source (clean text, chunked) and each note
page at import/write time, store vectors in SQLite — as BLOBs in a plain table
or via the `sqlite-vec` extension — and answer "find sources *about* X" even
when no word matches.

Unlocks:

- "related sources" on any source page
- "my notes that relate to this passage" from the reader
- agent tool `find_similar_sources(sid)`
- a lightweight RAG pipeline for the agent (§3)

**Sketch.**

- `integrations/llm.py`: `embed(texts) -> list[list[float]]` beside
  `chat_stream`, same client/retry policy; `kndb.yaml: llm.embedding_model`.
- `data/vectors.py`: chunk table `(id, source_id, note_id, ordinal, text,
  vec BLOB)`, cosine similarity computed in Python (fine up to ~10⁴ chunks) or
  `sqlite-vec` when it grows.
- `content/services.py`: embed on import (background task, so import stays
  fast) and on note save; route-level `related_sources` endpoint.

---

## 3. The agent: from a demo to a colleague

**Status today.** The agent is read-only and stateless: one message in, one
stream out (`POST /api/agent/message/{pid}` in `agent_routes.py`), tools are
`list_sources`, `search_sources_by_title/tags`, `get_source_content` (truncated
to 15 000 chars), `get_source_tags` (`content/agent.py`). No conversation
memory, no write actions, no links back into the app's data structures.

**The idea.** Three independent upgrades.

### 3a. Multi-turn conversations

Persist messages per `(project, user, conversation)` — a `conversations` and
`conversation_messages` table pair (schema of record in `db.py`, idempotent
migration). The route accepts a `conversation_id`; the frontend sends the id
instead of a lone message. The UI (agent pane template) gains a conversation
list, rename and delete. The agent's system prompt is already rebuilt per run
in `stream_agent`, so injecting a truncated history as prior turns is a small
change to `_text_turn`/`_native_turn` call sites in `content/services.py`.

### 3b. Write tools

Today's tools are read-only "so membership alone suffices". Write tools make
the agent a real assistant: "add this arXiv paper to the project and tag it
`tbc`", "create a board card titled X in column Y", "append a summary to the
note page of source S". The permission model already exists — tools simply
resolve the *user* the agent runs for and call `require_role` exactly like the
HTTP routes do, so a spectator's agent cannot write, by construction.

Natural first tools, in order of usefulness:

| Tool | Calls into |
|------|-----------|
| `add_source(url, tags)` | `fetchers.fetch_by_kind` + `store.create_source` + `projects.add_source` |
| `tag_source(sid, tags)` | `store` |
| `append_note(sid, text)` | `notes.write_lines` with the agent's name as author (blame shows it) |
| `create_card(column, title, ...)` | `data/board.py` |
| `create_quiz_question(sid, q, answers, idx)` | `services.add_quiz_question` |

Route change: the router must pass `user` into `services.stream_agent` so
tools can check roles; the SSE events already carry `toolcall` events, so the
frontend can show what the agent did. A confirmation step ("the agent wants
to X — allow?") is an optional frontend affordance.

### 3c. Grounded RAG answers with anchors

The signature idea — the natural convergence of §2 and everything KNDB already
does: when the agent answers from a source passage, the answer arrives
**grounded**: each claim carries a citation that is an actual KNDB **anchor**.

Flow: agent retrieves passages (FTS5/embeddings), the prompt (new template in
`prompts/`, e.g. `project_agent_rag.md`) asks for claims tagged with their
source ids and quoted spans, the backend locates each span in the document
(same locator machinery as `anchors.py`), creates real `NoteAnchor` rows
authored by the agent, and the SSE stream carries the anchor ids so the
frontend can render the citations as clickable highlights in the PDF/markdown
viewer — the existing `anchors.js` machinery does the rendering.

The result: LLM answers that are verifiable inside the source, persisted, and
clickable. No generic chat tool can do this; KNDB is uniquely shaped for it.

---

## 4. Spaced repetition: from Leitner to something serious

**Status today.** A fixed-interval Leitner box system: `_INTERVALS =
[0, 1, 2, 4, 7, 15, 30, 60]` days, per-question `box`, `streak`, `next_review`
(`content/services.py`). Questions are ordered due-first, then weakest
(`quiz_order`). There is a `/learning` page with per-source include/exclude and
an "only due" switch. Solid, but the scheduling is one-size-fits-all.

**The idea.** Make `/learning` the app's differentiator.

### 4a. A real scheduler (FSRS or SM-2)

Replace the fixed box ladder with an FSRS-style or SM-2 scheduler: each answer
updates an ease/difficulty parameter, intervals adapt per question. This is a
drop-in change in `record_answer` — the stats blob is a JSON dict already, so
adding `difficulty`, `stability` fields needs no schema change (keep `box` for
backward compatibility or migrate in `migrations.py`).

### 4b. A global "due today" dashboard

The review set is currently assembled by manually selecting sources. A
dashboard that answers "what should I review today, across everything" — due
counts per project, a one-click "start review" over all due questions — turns
the learning page from a per-source utility into a daily habit.

### 4c. Anchor-driven review — review in context

KNDB's unique asset is **anchors**: sentences in notes tied to highlighted
passages in sources. Nobody's flashcard system reviews in context:

- show the highlighted PDF/markdown passage, ask "what did you note here?",
  reveal the note sentence, self-grade
- MCQs generated *from the anchored passage* rather than the whole document
- reviewing happens with the document one click away — "show me the paper"

This reuses anchors, the PDF renderer (`pdfview.js`), quizzes, and the
scheduler. It is the feature that makes KNDB not-an-Anki-clone.

---

## 5. Export, backup, interoperability

**Status today.** Data lives in `data/kndb.db` + `data/sources/`. There is no
export, no backup tooling, and — a documented wart — source rows and blob
files are written in separate, non-atomic steps (`store.py` docstring admits
"nothing here is atomic").

**The idea.** For a self-hosted personal knowledge base, the answer to "can I
leave / can I lose it?" must be an unqualified yes.

- **Project export as plain markdown.** One command/endpoint: a folder tree of
  markdown files (note pages as `.md` with front-matter: source URL, tags,
  anchors as blockquote links), sources as files alongside. Obsidian-compatible,
  human-readable, no lock-in.
- **BibTeX export.** Natural for the arXiv audience — export a project's
  sources as a `.bib` file (title, authors, year, url, abstract). arXiv
  metadata is already fetched at import (`fetchers.py` gets it from the arXiv
  API), it just needs to be kept and re-serialized.
- **One-command backup.** `python -m backend.backup` (or a CLI arg to
  `app.py`): SQLite `VACUUM INTO` for a consistent snapshot + copy of
  `sources/`, optionally gzip + retention policy. An hour of work, and the
  single biggest peace-of-mind feature for self-hosters.
- **Atomic source writes.** Close the documented wart: write the blob to
  `<id>.<ext>.tmp`, fsync, rename; insert the row only after the file is in
  place; on delete, remove the file only after the row commit. Also make the
  `.txt` sidecar regeneration idempotent (it already is by nature).

---

## 6. Ingestion expansion

**Status today.** `content/fetchers.py` classifies a URL (arXiv, Hugging Face,
web) and dispatches to a fetcher returning
`{"source_type", "content", "title", "url", "text"?}`. Adding a kind is cheap
by design.

Ideas, roughly by audience fit:

- **DOI / Semantic Scholar / PubMed.** Completes the scientific trio with
  arXiv. DOI content-negotiation (`Accept: application/vnd.citation_styles...`)
  or the Semantic Scholar API gives metadata + often an open-access PDF link;
  reuse the arXiv fetcher's shape (metadata + PDF + clean text).
- **BibTeX import (batch).** Paste a `.bib`, get N sources at once (metadata
  now, PDFs fetched opportunistically). Combined with export, KNDB speaks the
  citation ecosystem's language on both ends.
- **Watched feeds / alerts.** An arXiv keyword or category feed checked
  periodically (a background task on lifespan + a `feeds` table) that
  auto-imports new papers into a project, marked "unreviewed". Turns KNDB from
  a library into a current-awareness tool — you open KNDB and KNDB has news
  for you.
- **EPUB.** Same shape as PDF: blob + clean text for LLM calls.
- **YouTube transcripts** — lecture/talk content joins the library and gets
  the full pipeline (notes, anchors, quizzes) like everything else.

Each idea is one fetcher function + a `classify_url` branch + tests in
`test_fetchers.py`'s style.

---

## 7. Smaller, pleasant ideas

**Interconnection & structure**

- **Links between sources** (cites / related / follows-up). A `source_links`
  table (from_id, to_id, kind) + a "related" section on the source panel; a
  simple graph view (canvas/SVG, no framework needed) is a nice extra.
- **`[[wikilinks]]` in notes.** Parse on render (`common.js`'s markdown
  pipeline) and on save to maintain backlinks; makes note pages a real web.
- **LLM-suggested tags at import.** One cheap LLM call with the clean text —
  tags are the most tedious metadata to maintain, and the model is already
  there. Offer as suggestions (the UI confirms), store like any tags.

**Deployment & ops**

- **Dockerfile + compose.** App + an OpenAI-compatible server (llama.cpp
  server or Ollama) as two services, `kndb.yaml` wired via environment
  substitution. One-command deployment for the exact audience README targets.
- **Command palette (Ctrl-K).** The frontend already centralizes actions in
  `common.js`/page scripts; a palette over sources, projects, notes and
  commands fits the keyboard-friendly, no-framework UI style.
- **`.txt` sidecar regeneration** as a maintenance command (rebuild all sidecars
  from blobs) — useful after fetcher improvements, and a building block for
  FTS5 indexing (§2a).

**Learning page polish**

- Per-question "edit / suspend / bury" controls on the review screen.
- Stats: reviews/day chart, retention rate, leech detection (questions answered
  wrong N times) — all derivable from the existing stats blobs.

---

*Each idea above is sized to be started independently; none depends on another
except where noted (§2b feeds §3c; §2a improves §3b's retrieval).*
