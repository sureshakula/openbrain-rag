# VOZIQ Brain v0.3

Personal RAG system on Mac Mini M4 24GB.

## Stack
- **Postgres + pgvector** — document store, chunk store, vector index
- **Ollama (nomic-embed-text)** — local embeddings (768-dim)
- **Claude API** — synthesis
- **BookStack** — wiki output (Phase 4)
- **MCP server** — Claude access layer (Phase 4)

## Architecture

```
Ingest → Chunk → Embed → Dedup → documents/chunks tables
                                        ↓
                                  synthesize.py
                                        ↓
                               synthesis_runs table
                                        ↓
                               review_queue (PENDING)
                                        ↓
                          ACCEPT → knowledge_base (RAG corpus)
                          REJECT → back to chunked (re-synthesize)
                          ABANDON → excluded
```

## Setup

```bash
# 1. Create DB
createdb voziq_brain
psql voziq_brain < db/schema.sql

# 2. Install deps
pip install -r requirements.txt

# 3. Configure
cp config.py config_local.py  # fill in DB creds + API key
export ANTHROPIC_API_KEY=sk-...
export VB_DB_PASSWORD=yourpassword

# 4. Ensure Ollama is running with nomic-embed-text
ollama pull nomic-embed-text
ollama serve
```

## Usage

```bash
# Phase 1: Ingest a folder
python ingestion/ingest_local.py --folder ~/Documents/notes

# Phase 2: Synthesize all chunked documents
python synthesis/synthesize.py

# Phase 3: Review
python review/review_queue.py --list
python review/review_queue.py --accept 1
python review/review_queue.py --reject 2 --note "too superficial"
python review/review_queue.py --abandon 3 --note "not relevant"
python review/review_queue.py --stats
```

## File Structure

```
voziq-brain/
├── config.py                  # env-driven config
├── requirements.txt
├── db/
│   ├── schema.sql             # run once to initialize
│   └── connection.py          # connection pool + sha256 helper
├── ingestion/
│   └── ingest_local.py        # local folder ingestion (Phase 1)
├── synthesis/
│   └── synthesize.py          # Claude synthesis → review_queue
└── review/
    └── review_queue.py        # ACCEPT / REJECT / ABANDON CLI
```

## Phases

| Phase | Component | Status |
|-------|-----------|--------|
| 1 | DB schema + synthesis pipeline + review queue | ✅ Built |
| 2 | Local folder ingestion | ✅ Built |
| 3 | Outlook (Graph API) ingestion | Next |
| 4 | Web UI ingestion | Planned |
| 5 | MCP server (RAG query layer) | Planned |
| 6 | BookStack write-back | Planned |

## Web UI

### One-time setup

```bash
docker compose up -d db                   # postgres + pgvector
./scripts/psql.sh openbrain      -f db/schema.sql
./scripts/psql.sh openbrain_test -f db/schema.sql
.venv/bin/pip install -r requirements.txt
mkdir -p uploads
```

### Run dev server (local Python)

```bash
export ANTHROPIC_API_KEY=sk-ant-...
.venv/bin/uvicorn webui.app:app --host 0.0.0.0 --port 8000
```

Workers start automatically via FastAPI lifespan. Open http://localhost:8000.

### Pages

- `/upload` — upload files (txt, md, pdf, docx, pptx)
- `/inventory` — live document list with status (polls every 5s)
- `/docgen` — generate a draft from a topic prompt
- `/drafts` — review queue with accept / reject / abandon
- `/health` — JSON health check

### Run tests

```bash
.venv/bin/pytest -v
```

(Requires docker-compose Postgres up and the test DB initialized as above.)
