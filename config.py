"""
VOZIQ Brain v0.3 — Config
Copy this to config.py and fill in your values. Never commit secrets.
"""

import os

# ── Postgres ──────────────────────────────────────────────
DB_HOST     = os.getenv("VB_DB_HOST", "localhost")
DB_PORT     = int(os.getenv("VB_DB_PORT", "5432"))
DB_NAME     = os.getenv("VB_DB_NAME", "voziq_brain")
DB_USER     = os.getenv("VB_DB_USER", "postgres")
DB_PASSWORD = os.getenv("VB_DB_PASSWORD", "")

# ── Ollama ────────────────────────────────────────────────
OLLAMA_BASE_URL     = os.getenv("VB_OLLAMA_URL", "http://localhost:11434")
EMBEDDING_MODEL     = os.getenv("VB_EMBED_MODEL", "nomic-embed-text")
EMBEDDING_DIM       = 768

# ── Claude API ────────────────────────────────────────────
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY", "")
SYNTHESIS_MODEL     = "claude-sonnet-4-20250514"
SYNTHESIS_MAX_TOKENS = 1024

# ── Chunking ──────────────────────────────────────────────
CHUNK_SIZE          = 400       # tokens per chunk (approximate, character-based)
CHUNK_OVERLAP       = 80        # overlap between consecutive chunks

# ── Review ────────────────────────────────────────────────
REVIEW_CHANNEL      = "outlook"  # 'outlook' | 'web_ui'

# ── Webui ─────────────────────────────────────────────────
WEBUI_HOST          = os.getenv("VB_WEBUI_HOST", "0.0.0.0")
WEBUI_PORT          = int(os.getenv("VB_WEBUI_PORT", "8000"))

# ── Upload / Worker ───────────────────────────────────────
UPLOAD_DIR          = os.getenv("VB_UPLOAD_DIR", "./uploads")
MAX_UPLOAD_MB       = int(os.getenv("VB_MAX_UPLOAD_MB", "500"))
INGEST_WORKER_COUNT = int(os.getenv("VB_INGEST_WORKER_COUNT", "2"))

# ── Retrieval ─────────────────────────────────────────────
RETRIEVAL_TOP_K     = int(os.getenv("VB_RETRIEVAL_TOP_K", "12"))
RETRIEVAL_RRF_K     = int(os.getenv("VB_RETRIEVAL_RRF_K", "60"))  # RRF constant

# ── BookStack ─────────────────────────────────────────────
BOOKSTACK_STUB      = os.getenv("VB_BOOKSTACK_STUB", "true").lower() == "true"
BOOKSTACK_URL       = os.getenv("VB_BOOKSTACK_URL", "")
BOOKSTACK_TOKEN     = os.getenv("VB_BOOKSTACK_TOKEN", "")
