# OpenBrain Webui — Design Spec

**Date:** 2026-05-24
**Status:** Draft, pre-implementation
**Source:** Brainstorming session with user (Suresh Akula)
**Parent spec:** `VOZIQ-Internal-Knowledge-RAG-System.txt` (v0.3) — Web UI section

## Purpose

Build the browser-based interface for OpenBrain RAG. Covers all five surfaces from the parent spec: document upload, ingestion status, document inventory, document generation, and draft review. Single internal user. Local network only.

## Scope decisions

| Question | Decision |
|----------|----------|
| Surfaces in v1 | All 5 (upload, status, inventory, doc-gen, drafts) |
| Stack | FastAPI + Jinja2 templates + HTMX |
| Auth | None. Bind `0.0.0.0` for LAN access. Trust network. |
| Doc-gen backend | Build retrieval + synthesis layer first (separate from existing `synthesis/synthesize.py`) |
| Review workflow | In-UI accept/reject/abandon buttons only. Skip email-driven review from parent spec. |
| Upload behavior | Async: upload returns immediately, background worker processes |
| Worker tech | FastAPI BackgroundTasks + `asyncio.Queue`. Single process. No Redis. |
| File size limit | 500MB (matches parent spec for MP4) |

## Out of scope (this spec)

These remain undone and are NOT addressed here:

- Outlook email ingestion (Source 3 from parent spec)
- Reviewer email dispatch + ACCEPT/REJECT reply parsing
- MP4 transcription engine — UI dispatches to a worker stub that marks file failed with "transcription not yet implemented"
- Authentication / multi-user access
- Full BookStack publish — UI's Accept button calls a stub that logs the payload and marks the draft accepted; real BookStack POST is a separate spec
- MCP server for live Q&A retrieval (parent spec Phase 4)

Anything in the parent spec not listed in "Scope decisions" or implemented by an existing module is out of scope for this build.

## Architecture

### Stack

- **Backend:** FastAPI on Uvicorn, single process, port 8000
- **Templates:** Jinja2 server-rendered HTML
- **Interactivity:** HTMX for partial swaps (status polling, upload progress, row updates, modal forms)
- **Static assets:** `htmx.min.js` vendored, single `app.css`
- **DB:** Existing Postgres + pgvector
- **No build pipeline.** No webpack, no node, no SPA.

### Module layout

New code added to repo:

```
webui/
  app.py                      # FastAPI app, lifespan, route mount, exception handlers
  routes/
    __init__.py
    upload.py                 # GET /upload (form), POST /upload (multipart)
    inventory.py              # GET /inventory, GET /inventory/rows (HTMX partial, polled)
    docgen.py                 # GET /docgen, POST /docgen/generate
    drafts.py                 # GET /drafts, GET /drafts/{id}, POST /drafts/{id}/{accept|reject|abandon}
  workers/
    __init__.py
    ingest_queue.py           # asyncio.Queue + worker coroutine started in lifespan
    file_dispatch.py          # route file by extension to extractor (txt/md/pdf/docx/pptx/mp4)
  templates/
    base.html                 # left-sidebar layout, nav, slot for page content
    upload.html
    inventory.html
    _inventory_rows.html      # HTMX partial — just the <tbody> rows
    docgen.html
    _docgen_draft.html        # HTMX partial — preview pane after generate
    drafts.html
    _draft_detail.html        # HTMX partial — right pane when draft selected
    _reject_form.html         # HTMX partial — feedback textarea modal
  static/
    htmx.min.js
    app.css

retrieval/
  __init__.py
  search.py                   # hybrid pgvector ANN + Postgres tsvector BM25, returns top-K chunks

synthesis/
  generate.py                 # NEW: topic + filters → retrieve → Claude → draft.
                              # Separate from existing synthesize.py (which does per-doc full-text synthesis).

publish/
  bookstack_stub.py           # logs payload, returns OK. Real impl is separate spec.

uploads/                      # gitignored. Raw uploaded files land here before ingestion.
```

Existing code (`config.py`, `db/`, `ingestion/ingest_local.py`, `synthesis/synthesize.py`, `review/review_queue.py`) is NOT modified by this spec beyond:
- `ingestion/ingest_local.py` is refactored so its chunk-and-embed core is importable by `webui/workers/ingest_queue.py` — the CLI entry point stays.

### Why these boundaries

Each module has one job and a small public surface:

- `webui/routes/*` — HTTP only. No DB writes outside calling `db/` or worker queue.
- `webui/workers/ingest_queue.py` — pulls from queue, calls `file_dispatch`, writes status to `documents` row. Knows nothing about HTTP.
- `webui/workers/file_dispatch.py` — pure function: bytes + filename → extracted text + metadata. No DB.
- `retrieval/search.py` — query string + filters → list of chunks. No HTTP, no template.
- `synthesis/generate.py` — topic + chunks + doc type → draft markdown + citation list. No HTTP, no DB writes (returns data; route stores).

This means each piece can be tested with a small fixture and changed without ripple.

## Data flow

### Upload → ingest

```
Browser ─POST /upload─▶ upload.py
                          │
                          ├─ write file to uploads/<sha256>.<ext>
                          ├─ INSERT documents (status='queued', sha256, filename, size)
                          ├─ ingest_queue.put(doc_id)
                          └─ 202 Accepted, HTMX redirect to /inventory

ingest_queue worker (background coroutine):
  loop:
    doc_id = await queue.get()
    UPDATE documents SET status='processing'
    text, meta = file_dispatch(file_path)
    chunks = chunk(text)
    embeddings = ollama_embed(chunks)
    INSERT chunks ...
    UPDATE documents SET status='indexed', chunk_count=N
  on exception:
    UPDATE documents SET status='failed', failure_reason=<short msg>
```

### Inventory polling

```
Browser GET /inventory                  # full page render
  template includes:
    <tbody hx-get="/inventory/rows"
           hx-trigger="every 5s"
           hx-swap="innerHTML"
           hx-include="[name='filter']">
       ... initial rows server-rendered ...
    </tbody>

Browser ─poll /inventory/rows every 5s─▶ inventory.py
                                            └─ returns _inventory_rows.html partial
```

### Doc-gen → draft

```
Browser ─POST /docgen/generate─▶ docgen.py
   form fields: topic, doc_type, source_filter (json)
                                    │
                                    ├─ chunks = retrieval.search(topic, filters, top_k=12)
                                    ├─ draft_md, citations = synthesis.generate(topic, doc_type, chunks)
                                    ├─ INSERT drafts (status='pending', body=draft_md, ...)
                                    ├─ INSERT draft_citations (draft_id, chunk_id, index)
                                    └─ return _docgen_draft.html partial
                                        (rendered into right pane via hx-swap)
```

### Draft review actions

```
Browser ─POST /drafts/{id}/accept─▶ drafts.py
                                      ├─ publish.bookstack_stub.publish(draft)
                                      ├─ UPDATE drafts SET status='accepted', reviewed_at=now()
                                      └─ HTMX swap row in queue + clear right pane

Browser ─POST /drafts/{id}/reject─▶ drafts.py
   form field: feedback
                                      ├─ UPDATE drafts SET status='rejected',
                                                            feedback=<text>,
                                                            reviewed_at=now()
                                      └─ HTMX swap row + show toast

Browser ─POST /drafts/{id}/abandon─▶ drafts.py
                                      ├─ UPDATE drafts SET status='abandoned'
                                      └─ HTMX swap row + clear right pane
```

## Database changes

Existing schema uses `SERIAL` PKs and `documents.status` as plain `TEXT`. Stay consistent — no UUIDs, no Postgres ENUM types, status stays TEXT with expanded allowed values enforced in app code (matches existing pattern).

New tables:

```sql
CREATE TABLE IF NOT EXISTS drafts (
  id             SERIAL PRIMARY KEY,
  topic          TEXT NOT NULL,
  doc_type       TEXT NOT NULL,
  body_markdown  TEXT NOT NULL,
  source_filter  JSONB,
  status         TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'accepted' | 'rejected' | 'abandoned'
  feedback       TEXT,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  reviewed_at    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS draft_citations (
  draft_id        INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
  chunk_id        INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
  citation_index  INTEGER NOT NULL,
  PRIMARY KEY (draft_id, citation_index)
);

CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);
CREATE INDEX IF NOT EXISTS idx_drafts_created ON drafts(created_at DESC);
```

Existing `documents` table extended. `status` already exists as TEXT — add new allowed values: `'queued' | 'processing' | 'transcribing' | 'indexed' | 'failed'` (keep existing `'pending' | 'chunked' | 'synthesized' | 'failed'` as legacy for the existing CLI ingester; the webui worker uses the new set).

```sql
ALTER TABLE documents
  ADD COLUMN IF NOT EXISTS failure_reason TEXT,
  ADD COLUMN IF NOT EXISTS active         BOOLEAN NOT NULL DEFAULT TRUE,
  ADD COLUMN IF NOT EXISTS file_size      BIGINT,
  ADD COLUMN IF NOT EXISTS file_extension TEXT;

CREATE INDEX IF NOT EXISTS idx_documents_active ON documents(active);
```

(`idx_documents_status` already exists in current schema.)

New schema additions appended to `db/schema.sql` (idempotent `IF NOT EXISTS` matches existing file pattern). No migration runner needed — re-running `psql -f db/schema.sql` is safe.

## Retrieval layer

`retrieval/search.py`:

```python
def search(
    query: str,
    filters: SearchFilters | None = None,
    top_k: int = 12,
) -> list[Chunk]:
    """Hybrid: pgvector cosine ANN + Postgres tsvector BM25, RRF-merged."""
```

- Embed `query` via Ollama
- Run two queries:
  1. `SELECT ... ORDER BY embedding <=> $1 LIMIT $k * 2`  (vector)
  2. `SELECT ... WHERE to_tsvector('english', text) @@ plainto_tsquery($1)`  (BM25)
- Reciprocal rank fusion merge → top-K
- Filters apply as WHERE on `source_type`, `file_extension`, `content_date` range, `active = TRUE`

If `filters.source_filter` is empty, search all active chunks.

## Synthesis layer

`synthesis/generate.py`:

```python
DOC_TYPE_TEMPLATES = {
    "process":           "...",
    "architecture":      "...",
    "meeting_summary":   "...",
    "decision_record":   "...",
    "adr":               "...",
    "release_note":      "...",
}

def generate(
    topic: str,
    doc_type: str,
    chunks: list[Chunk],
) -> tuple[str, list[Citation]]:
    """Build prompt, call Claude, return (markdown_body, citations)."""
```

- Prompt template per doc type
- Inject chunks as numbered context blocks `[1] ... [2] ...`
- Instruct Claude to cite using `[N]` markers
- Parse returned markdown, extract citations actually used (drop unreferenced chunks)
- Return body + citations in citation order

## Error handling

| Failure | UI behavior |
|---------|-------------|
| Upload >500MB | 413 response; HTMX swaps error banner into upload page |
| Upload IO error | 500; flash message; file not in DB |
| Ingest worker exception | `documents.status='failed'`, `failure_reason` set; shown in inventory; row click opens detail modal with full reason |
| Ollama unreachable during ingest | failure_reason="embedding service unreachable"; retry button on row |
| Ollama unreachable during retrieval | docgen returns error toast, no draft row created |
| Claude API error on generate | docgen preview pane shows error + retry; no draft row |
| BookStack stub error on accept | draft stays pending; toast; retry button |
| File type not supported | failure_reason="unsupported file type: .xyz" |
| MP4 file uploaded | failure_reason="transcription not yet implemented" |

No silent failures. Every error path writes a row the user can see.

## Testing

- `pytest` + `httpx.AsyncClient` for route tests
- Real Postgres (testcontainers or local test DB) — no DB mocks (matches existing project pattern)
- Mock Ollama, Claude API, BookStack stub at the HTTP boundary using `respx`
- Test files mirror module layout: `tests/webui/routes/test_upload.py`, `tests/retrieval/test_search.py`, etc.
- Coverage target: every route has a happy path test + at least one error path test

## Configuration additions

`config.py` gains:

```python
WEBUI_HOST          = env("WEBUI_HOST", "0.0.0.0")
WEBUI_PORT          = env_int("WEBUI_PORT", 8000)
UPLOAD_DIR          = env_path("UPLOAD_DIR", "./uploads")
MAX_UPLOAD_MB       = env_int("MAX_UPLOAD_MB", 500)
INGEST_WORKER_COUNT = env_int("INGEST_WORKER_COUNT", 2)
RETRIEVAL_TOP_K     = env_int("RETRIEVAL_TOP_K", 12)
BOOKSTACK_STUB      = env_bool("BOOKSTACK_STUB", True)
```

`requirements.txt` adds: `fastapi`, `uvicorn[standard]`, `jinja2`, `python-multipart`, `httpx`, `respx` (dev), `pytest`, `pytest-asyncio`.

PDF/DOCX/PPTX extractors add: `pymupdf`, `python-docx`, `python-pptx`. (Even though MP4 is stubbed, these are needed for the upload page to be useful end-to-end.)

## Open questions (none blocking)

None at spec-write time. All scope decisions resolved in brainstorming.
