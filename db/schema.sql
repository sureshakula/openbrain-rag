-- VOZIQ Brain v0.3 — Core Schema
-- Run once against your Postgres instance
-- Requires: pgvector extension

CREATE EXTENSION IF NOT EXISTS vector;

-- ─────────────────────────────────────────
-- DOCUMENTS
-- Raw ingested sources before chunking
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS documents (
    id              SERIAL PRIMARY KEY,
    source_type     TEXT NOT NULL,          -- 'local_file' | 'web_ui' | 'outlook_email'
    source_ref      TEXT NOT NULL,          -- file path, URL, or outlook message_id
    title           TEXT,
    content_hash    TEXT NOT NULL UNIQUE,   -- SHA-256 of raw content — dedup gate
    raw_content     TEXT NOT NULL,
    metadata        JSONB DEFAULT '{}',     -- source-specific extras (thread_id, sender, etc.)
    ingested_at     TIMESTAMPTZ DEFAULT NOW(),
    status          TEXT DEFAULT 'pending'  -- 'pending' | 'chunked' | 'synthesized' | 'failed'
);

CREATE INDEX IF NOT EXISTS idx_documents_source_type ON documents(source_type);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
CREATE INDEX IF NOT EXISTS idx_documents_content_hash ON documents(content_hash);

-- ─────────────────────────────────────────
-- CHUNKS
-- Chunked + embedded fragments of documents
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS chunks (
    id              SERIAL PRIMARY KEY,
    document_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index     INTEGER NOT NULL,       -- position within document
    content         TEXT NOT NULL,
    content_hash    TEXT NOT NULL,          -- SHA-256 of chunk text — dedup within doc
    token_count     INTEGER,
    embedding       vector(768),            -- nomic-embed-text dimension
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_content_hash ON chunks(content_hash);
-- pgvector HNSW index for fast ANN search
CREATE INDEX IF NOT EXISTS idx_chunks_embedding ON chunks
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- ─────────────────────────────────────────
-- SYNTHESIS RUNS
-- One row per synthesis attempt on a document
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS synthesis_runs (
    id              SERIAL PRIMARY KEY,
    document_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    model           TEXT NOT NULL,          -- e.g. 'claude-sonnet-4-20250514'
    prompt_tokens   INTEGER,
    completion_tokens INTEGER,
    synthesis_text  TEXT NOT NULL,
    synthesis_hash  TEXT NOT NULL,          -- SHA-256 of output — detect regeneration drift
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_synthesis_document_id ON synthesis_runs(document_id);

-- ─────────────────────────────────────────
-- REVIEW QUEUE
-- Synthesis candidates waiting for ACCEPT/REJECT/ABANDON
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS review_queue (
    id              SERIAL PRIMARY KEY,
    synthesis_run_id INTEGER NOT NULL REFERENCES synthesis_runs(id) ON DELETE CASCADE,
    document_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    status          TEXT NOT NULL DEFAULT 'PENDING',  -- 'PENDING' | 'ACCEPTED' | 'REJECTED' | 'ABANDONED'
    review_channel  TEXT DEFAULT 'outlook',           -- 'outlook' | 'web_ui'
    outlook_msg_id  TEXT,                             -- set when dispatched to Outlook
    reviewer_note   TEXT,                             -- populated on review
    queued_at       TIMESTAMPTZ DEFAULT NOW(),
    reviewed_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_review_queue_status ON review_queue(status);
CREATE INDEX IF NOT EXISTS idx_review_queue_document_id ON review_queue(document_id);

-- ─────────────────────────────────────────
-- KNOWLEDGE BASE
-- Accepted synthesis entries — the actual RAG corpus
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS knowledge_base (
    id              SERIAL PRIMARY KEY,
    review_queue_id INTEGER NOT NULL REFERENCES review_queue(id),
    document_id     INTEGER NOT NULL REFERENCES documents(id),
    synthesis_run_id INTEGER NOT NULL REFERENCES synthesis_runs(id),
    title           TEXT,
    summary         TEXT NOT NULL,          -- the accepted synthesis text
    source_type     TEXT NOT NULL,
    source_ref      TEXT NOT NULL,
    metadata        JSONB DEFAULT '{}',
    accepted_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_knowledge_base_source_type ON knowledge_base(source_type);

-- ─────────────────────────────────────────
-- WEBUI: DRAFTS
-- Topic-prompted drafts generated via doc-gen UI (distinct from synthesis_runs)
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS drafts (
    id              SERIAL PRIMARY KEY,
    topic           TEXT NOT NULL,
    doc_type        TEXT NOT NULL,                -- 'process'|'architecture'|'meeting_summary'|'decision_record'|'adr'|'release_note'
    body_markdown   TEXT NOT NULL,
    source_filter   JSONB,
    status          TEXT NOT NULL DEFAULT 'pending', -- 'pending'|'accepted'|'rejected'|'abandoned'
    feedback        TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_drafts_status  ON drafts(status);
CREATE INDEX IF NOT EXISTS idx_drafts_created ON drafts(created_at DESC);

-- ─────────────────────────────────────────
-- WEBUI: DRAFT CITATIONS
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS draft_citations (
    draft_id        INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    chunk_id        INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    citation_index  INTEGER NOT NULL,
    PRIMARY KEY (draft_id, citation_index)
);

-- ─────────────────────────────────────────
-- WEBUI: DOCUMENTS EXTENSIONS
-- ─────────────────────────────────────────
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS failure_reason TEXT,
    ADD COLUMN IF NOT EXISTS active         BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS file_size      BIGINT,
    ADD COLUMN IF NOT EXISTS file_extension TEXT,
    ADD COLUMN IF NOT EXISTS namespace      TEXT NOT NULL DEFAULT 'general';

CREATE INDEX IF NOT EXISTS idx_documents_active    ON documents(active);
CREATE INDEX IF NOT EXISTS idx_documents_namespace ON documents(namespace);

-- ─────────────────────────────────────────
-- WEBUI: BM25 SUPPORT
-- Generated tsvector column on chunks for keyword search (hybrid retrieval)
-- ─────────────────────────────────────────
ALTER TABLE chunks
    ADD COLUMN IF NOT EXISTS content_tsv tsvector
        GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;

CREATE INDEX IF NOT EXISTS idx_chunks_content_tsv ON chunks USING GIN (content_tsv);

-- ─────────────────────────────────────────
-- MULTI-USER SPACES
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id          SERIAL PRIMARY KEY,
    username    TEXT NOT NULL UNIQUE,
    mcp_token   TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS spaces (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('personal','shared')),
    created_by  INTEGER REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- One shared space per name (case-insensitive). Personal names are not unique.
CREATE UNIQUE INDEX IF NOT EXISTS idx_spaces_shared_name
    ON spaces (lower(name)) WHERE kind = 'shared';

CREATE TABLE IF NOT EXISTS space_members (
    space_id    INTEGER NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL REFERENCES users(id)  ON DELETE CASCADE,
    role        TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('owner','member')),
    PRIMARY KEY (space_id, user_id)
);

-- Seed the built-in Common shared space (created_by NULL).
INSERT INTO spaces (name, kind, created_by)
SELECT 'Common', 'shared', NULL
WHERE NOT EXISTS (
    SELECT 1 FROM spaces WHERE kind = 'shared' AND lower(name) = 'common'
);
