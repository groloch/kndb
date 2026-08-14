# KNDB — Knowledge Database

A self-hosted, all-in-one personal knowledge database with a web UI. It imports
scientific papers, websites and blog posts, links markdown notes to each source, and integrates LLM-based features to help manage everything.

## Features

- **Import from anywhere** — arXiv papers, Hugging Face model cards, arbitrary web pages (archived with styles and images), and local `.pdf` / `.md` files. Re-importing a URL deduplicates automatically.
- **Markdown notes** — attach personal notes to every source
- **Automated quiz generation** — generate multiple-choice quizzes from a source's content or notes, with configurable count, difficulty, and language.
- **Spaced repetition** — to help remembering the essential in sources.
- **Built-in summarization** — summarize any source and have the result appended directly to the source's notes.
- **Projects** — organize sources into named projects with other people.
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
`http://127.0.0.1:8000/v1`. **All configuration lives in [kndb.yaml](kndb.yaml)** (see that file for the full option list):

## How it stores data

Everything but the source documents lives in one **SQLite database** (`data/kndb.db`). Source documents stay on disk:

```
data/
  kndb.db              # sources, projects, notes, anchors, quizzes — SQLite
  sources/<id>.<ext>   # the fetched/uploaded document (pdf / md / html)
  sources/<id>.txt     # optional clean-text rendition, used for LLM calls
```
