# KNDB — Knowledge Database

A self-hosted, all-in-one personal knowledge database with a web UI. It imports
scientific papers, websites and blog posts, links markdown notes to each source,
and integrates LLM-based features to help manage everything.

## Features

- **Import from anywhere** — arXiv papers, Hugging Face model cards, arbitrary web pages (archived with styles and images), and local `.pdf` / `.md` files. Re-importing a URL deduplicates automatically.
- **Markdown notes** — attach personal notes to every source
- **Automated quiz generation** — generate multiple-choice quizzes from a source's content or notes, with configurable count, difficulty, and language.
- **Spaced repetition** — to help remembering the essential in sources.
- **Built-in summarization** — summarize any source and have the result appended directly to the source's notes.
- **Projects** — organize sources into named projects with other people.
- **Kanban board** — each project has a work board: columns with WIP limits, cards with labels, assignees and due dates, drag & drop, archiving, on its own tab.
- **Search** — filter sources by title substring, tag (`@tag`), or project (`/project`).
- **Bring your own LLM** — works with any OpenAI-compatible server (vLLM, llama.cpp, Ollama, OpenAI, etc.). No model runs inside KNDB.

## Quick start

```bash
# 0. Edit the kndb.yaml file to configure the app as you wish

# 1. create the virtualenv (once)
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt

# 2. install the frontend's third-party JS (once — pdf.js, marked, DOMPurify)
python dev_tools/fetch_vendor.py

# 3. run an OpenAI-compatible LLM server (any of these, for example vLLM):
vllm serve LFM2.5-1.2B-Instruct --port 8000

# 4. run the app
.venv/Scripts/python app.py

# 5. Browse to localhost:5000
```

Step 2 downloads three pinned packages from the npm registry into
`static/vendor/` (git-ignored) and checks each tarball against a SHA-256 in the
script. It needs no Node and no packages of its own — plain Python — and it is
the only network step: once it has run, KNDB's interface works offline.

By default the app expects an OpenAI-compatible server at
`http://127.0.0.1:8000/v1`. All configuration lives in [kndb.yaml](kndb.yaml).

## Authentication and account safety

The default `auth.mode: dev` exists only for local development. It permits the
user switcher and header identity and is **not authentication**. Do not expose
a dev-mode instance to an untrusted network.

To enable password accounts:

1. Install the pinned dependencies with `pip install -r requirements.txt`.
2. Generate an independent secret pepper and keep it in a secrets manager or
   protected service environment, never in `kndb.yaml` or source control:

   ```bash
   python -c 'import secrets; print(secrets.token_urlsafe(48))'
   export KNDB_AUTH_PEPPER='paste-the-generated-value-here'
   ```

   Keep a secure backup of this value. If it is lost or changed, stored
   password hashes and server-side session digests cannot be verified. Rotate
   it only with a planned password reset/migration.
3. Set `auth.mode: password`, `auth.cookie_secure: true`, and optionally
   `auth.allow_registration: true` in `kndb.yaml`. Registration is disabled by
   default; enabling it allows anyone who can reach the public landing page to
   create an account. Turn it off again whenever open signups are not intended.
   Serve KNDB only over HTTPS (including the browser-to-reverse-proxy
   connection). Password mode refuses to start with insecure cookies.
4. With registration disabled, create or provision an account interactively;
   passwords are prompted rather than placed in shell history:

   ```bash
   python -m backend.auth_admin alice
   ```

   This command also provisions legacy name-only users without changing their
   authored data. Use the existing username when assigning credentials to a
   legacy identity. It may also create a new account. Project member management
   in password mode only accepts existing accounts.
5. Start the server. Signed-out visitors see the landing page at `/`; existing
   accounts can sign in at `/login`, or use the create-account link when open
   registration is enabled. Passwords must be at least 14 characters. KNDB
   stores an Argon2id hash over a pepper-keyed password input, with a fresh
   random salt per hash. It does not store plaintext passwords or
   use a fast double-SHA password construction. Password mode uses opaque,
   revocable session cookies and CSRF tokens. Failed logins are rate-limited
   per account/client bucket; also use a rate-limiting proxy for defense in depth.

Passwords are not reversible. There is no email-based password reset yet; an
operator must use the provisioning command to reset a lost password. Password
mode and login are not a substitute for route-level project authorization,
secure backups, safe handling of imported HTML, or network-fetch protections.
Review the security guidance in `_docs` before exposing a deployment.

## How it stores data

Everything but the source documents lives in one **SQLite database** (`data/kndb.db`). Source documents stay on disk:

```
data/
  kndb.db              # sources, projects, notes, anchors, quizzes, boards — SQLite
  sources/<id>.<ext>   # the fetched/uploaded document (pdf / md / html)
  sources/<id>.txt     # optional clean-text rendition, used for LLM calls
```
