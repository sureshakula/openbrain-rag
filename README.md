# OpenBrain RAG

Internal RAG system for VOZIQ. Ingests scattered company knowledge (local files, uploads, soon email/MP4), serves it via semantic search, and uses Claude to generate documentation routed through a human review queue.

Single-machine deployment. Mac Mini M4 24GB target.

## Stack

- **FastAPI + Jinja + HTMX** — webui (5 pages, no JS build)
- **Postgres 16 + pgvector** — documents, chunks, drafts, hybrid retrieval (HNSW + tsvector BM25, RRF-merged)
- **Ollama (`nomic-embed-text`)** — local embeddings (768-dim)
- **Claude API** — draft generation with inline citations
- **psycopg2 + asyncio queue** — in-process upload worker pool

## Quickstart (Docker Compose, recommended)

```bash
git clone <this repo>
cd openbrain-rag

cp .env.example .env                        # optional; defaults work
# Set ANTHROPIC_API_KEY in .env if you want doc-gen to work

# Bring up Postgres + app. Postgres data persists in ./pgdata, uploads in ./uploads
docker compose up --build
```

Open http://localhost:8000.

Ollama is **not** in compose — it usually wants host GPU/Metal. Run on the host:

```bash
ollama serve
ollama pull nomic-embed-text
```

The app reaches Ollama via `host.docker.internal:11434` (override with `VB_OLLAMA_URL`).

If Docker Desktop's keychain is locked on macOS, prefix every docker command with `env DOCKER_CONFIG=/tmp/docker-noauth` (the repo creates that config on first use via `scripts/psql.sh`).

Stop:

```bash
docker compose down          # keep volumes
rm -rf pgdata uploads        # also wipe data
```

## Webui pages

| Route | Purpose |
|-------|---------|
| `/upload` | Drop files (.txt .md .rst .py .sql .csv .pdf .docx .pptx; .mp4 accepted but transcription stubbed) up to 500MB. Queues ingestion. |
| `/inventory` | Live document list with status (queued/processing/indexed/failed). Polls every 5s. Filter by status/source/title. |
| `/docgen` | Topic prompt + doc type + optional source filter → hybrid retrieval → Claude → draft with inline `[N]` citations. |
| `/drafts` | Review queue with in-UI Accept (→ BookStack stub) / Reject (with feedback) / Abandon actions. |
| `/health` | JSON health check. |

## Run without Docker (local Python)

Requires Python 3.11+ and a local Postgres with `pgvector` extension.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

createdb openbrain
psql openbrain      -c "CREATE EXTENSION IF NOT EXISTS vector;"
psql openbrain      -f db/schema.sql

createdb openbrain_test
psql openbrain_test -c "CREATE EXTENSION IF NOT EXISTS vector;"
psql openbrain_test -f db/schema.sql

mkdir -p uploads

export ANTHROPIC_API_KEY=sk-ant-...
export VB_DB_PASSWORD=...
export VB_DB_NAME=openbrain
.venv/bin/uvicorn webui.app:app --host 0.0.0.0 --port 8000
```

Workers start via FastAPI lifespan.

## CLI tools (alternative to webui)

```bash
# Ingest a folder (txt/md/rst/py/sql/csv only — PDF/DOCX go through webui)
python ingestion/ingest_local.py --folder ~/Documents/notes
python ingestion/ingest_local.py --folder ~/Documents/notes --dry-run

# Per-document synthesis (different from /docgen, which is topic-prompted)
python synthesis/synthesize.py

# Review queue (separate from webui drafts queue)
python review/review_queue.py --list
python review/review_queue.py --accept 1
python review/review_queue.py --reject 2 --note "too superficial"
python review/review_queue.py --abandon 3 --note "not relevant"
python review/review_queue.py --stats
```

## Run tests

```bash
docker compose up -d db                                # if not already running
./scripts/psql.sh openbrain_test -f db/schema.sql      # idempotent

.venv/bin/pytest -v
```

Tests cover: file dispatch, ingest queue worker, retrieval, synthesis-generate, BookStack stub, every webui route, plus an end-to-end smoke (`tests/test_smoke_e2e.py`) that drives upload → worker → inventory → docgen → accept against the real Postgres. Claude and Ollama are mocked at the HTTP boundary.

## Architecture

```
              ┌──────────────────────────────┐
upload  ───▶  │  webui/routes/upload.py      │  ──▶  documents (status='queued')
              └──────────────────────────────┘                │
                                                              ▼
                                            ┌──────────────────────────────┐
                                            │  webui/workers/ingest_queue  │
                                            │  asyncio.Queue + 2 workers   │
                                            └──────────────────────────────┘
                                                              │
                                                              ▼
                              file_dispatch ─▶ chunk ─▶ Ollama embed ─▶ chunks
                                                              │
                                                              ▼
                                                  status='indexed'

topic prompt  ─▶  retrieval/search.py  (pgvector HNSW + tsvector BM25 → RRF)
                          │
                          ▼
              synthesis/generate.py  (Claude API, [N] citations)
                          │
                          ▼
                      drafts + draft_citations
                          │
       ┌──────────────────┼──────────────────┐
       ▼                  ▼                  ▼
  Accept → publish    Reject + feedback   Abandon
  (BookStack stub)    (visible in UI)
```

## File layout

```
openbrain-rag/
├── config.py                       # env-driven config
├── requirements.txt
├── docker-compose.yml              # postgres + app
├── Dockerfile                      # python:3.11-slim + psql client
├── pyproject.toml                  # pytest config
│
├── db/
│   ├── schema.sql                  # idempotent — run anytime
│   └── connection.py               # psycopg2 pool, sha256
│
├── ingestion/
│   ├── core.py                     # shared chunk/embed/insert helpers
│   └── ingest_local.py             # CLI ingester
│
├── retrieval/
│   └── search.py                   # hybrid pgvector + BM25 + RRF
│
├── synthesis/
│   ├── synthesize.py               # per-document synthesis (CLI)
│   └── generate.py                 # topic → draft (webui)
│
├── publish/
│   └── bookstack_stub.py           # logs payload, returns fake URL
│
├── review/
│   └── review_queue.py             # CLI review for synthesis_runs
│
├── webui/
│   ├── app.py                      # FastAPI factory, lifespan, render helper
│   ├── routes/
│   │   ├── upload.py
│   │   ├── inventory.py
│   │   ├── docgen.py
│   │   └── drafts.py
│   ├── workers/
│   │   ├── ingest_queue.py         # asyncio worker pool
│   │   └── file_dispatch.py        # txt/md/pdf/docx/pptx + mp4 stub
│   ├── templates/                  # base + page templates + HTMX partials
│   └── static/                     # htmx.min.js + app.css
│
├── tests/                          # pytest, real Postgres, mocked Ollama/Claude
├── docker/
│   └── postgres-init/              # creates pgvector ext + openbrain_test DB
└── docs/
    └── superpowers/
        ├── specs/                  # design specs
        └── plans/                  # implementation plans
```

## Status

| Component | Status |
|-----------|--------|
| Webui (upload, inventory, doc-gen, drafts) | ✅ |
| Local file CLI ingestion (txt/md/rst/py/sql/csv) | ✅ |
| Webui ingestion (txt/md/pdf/docx/pptx) | ✅ |
| Hybrid retrieval (pgvector HNSW + tsvector BM25, RRF) | ✅ |
| Topic-prompted doc generation (Claude + citations) | ✅ |
| In-UI review actions + BookStack stub | ✅ |
| Docker compose (db + app, persistent volumes) | ✅ |
| Outlook email ingestion (Graph API) | Not built |
| Reviewer email dispatch + ACCEPT/REJECT reply parsing | Not built |
| MP4 transcription (faster-whisper) | Stubbed (marks failed) |
| Auth | Not built — localhost / LAN only |
| Real BookStack publish | Stubbed (logs payload) |
| MCP server (live Q&A retrieval) | Not built |

## Config (env vars)

| Var | Default | Purpose |
|-----|---------|---------|
| `VB_DB_HOST` | `localhost` | Postgres host (compose sets to `db`) |
| `VB_DB_PORT` | `5432` | |
| `VB_DB_NAME` | `voziq_brain` | Compose sets to `openbrain` |
| `VB_DB_USER` | `postgres` | |
| `VB_DB_PASSWORD` | `` | Compose sets to `postgres` |
| `VB_OLLAMA_URL` | `http://localhost:11434` | Compose sets to `http://host.docker.internal:11434` |
| `VB_UPLOAD_DIR` | `./uploads` | Compose sets to `/data/uploads` |
| `VB_MAX_UPLOAD_MB` | `500` | Reject larger uploads with 413 |
| `VB_INGEST_WORKER_COUNT` | `2` | asyncio worker pool size |
| `VB_RETRIEVAL_TOP_K` | `12` | Final chunks passed to Claude |
| `VB_RETRIEVAL_RRF_K` | `60` | RRF constant |
| `VB_BOOKSTACK_STUB` | `true` | Use stub publisher (only stub is implemented) |
| `ANTHROPIC_API_KEY` | `` | Required for doc-gen |

## Docs

- `docs/superpowers/specs/2026-05-24-webui-design.md` — design spec for the webui phase
- `docs/superpowers/plans/2026-05-24-webui.md` — implementation plan (17 tasks)
- `VOZIQ-Internal-Knowledge-RAG-System.txt` — parent requirements doc (v0.3)
