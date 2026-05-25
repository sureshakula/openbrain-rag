"""Reusable ingestion helpers: chunking, embedding, DB writes.

Shared by the CLI ingester (ingest_local.py) and the webui async worker.
"""
import hashlib
import re
import time
import psycopg2.extras
import requests

from config import CHUNK_SIZE, CHUNK_OVERLAP, OLLAMA_BASE_URL, EMBEDDING_MODEL


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE,
               overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Paragraph-first chunker, falls back to character windows for oversize paragraphs."""
    char_size = chunk_size * 4
    char_overlap = overlap * 4
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if len(current) + len(para) + 2 <= char_size:
            current = (current + "\n\n" + para).strip()
        else:
            if current:
                chunks.append(current)
                current = ""
            if len(para) > char_size:
                step = max(1, char_size - char_overlap)
                for i in range(0, len(para), step):
                    chunks.append(para[i:i + char_size])
            else:
                current = para
    if current:
        chunks.append(current)
    return chunks


def embed(text: str, timeout: int = 30) -> list[float] | None:
    """Call Ollama for an embedding. Returns None on failure."""
    try:
        resp = requests.post(
            f"{OLLAMA_BASE_URL}/api/embeddings",
            json={"model": EMBEDDING_MODEL, "prompt": text},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()["embedding"]
    except Exception:
        return None


DEFAULT_NAMESPACE = "general"

# Suggested namespaces shown in upload UI dropdown. Free-form: any string allowed.
SUGGESTED_NAMESPACES = [
    "general", "code", "operations", "product", "sales_marketing", "company_hr",
]


def insert_document(conn, *, source_type: str, source_ref: str, title: str,
                    raw_content: str, content_hash: str, file_size: int | None,
                    file_extension: str | None, status: str,
                    namespace: str = DEFAULT_NAMESPACE,
                    metadata: dict | None = None) -> int:
    """Insert a document row. Returns new document id."""
    metadata = metadata or {}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO documents
                (source_type, source_ref, title, content_hash, raw_content,
                 metadata, status, file_size, file_extension, namespace)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (source_type, source_ref, title, content_hash, raw_content,
             psycopg2.extras.Json(metadata), status, file_size, file_extension,
             namespace or DEFAULT_NAMESPACE),
        )
        return cur.fetchone()[0]


def document_exists(conn, content_hash: str) -> int | None:
    """Return id of existing document with this hash, or None."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM documents WHERE content_hash = %s", (content_hash,))
        row = cur.fetchone()
        return row[0] if row else None


def insert_chunks(conn, document_id: int, chunks: list[str],
                  embeddings: list[list[float] | None]) -> None:
    with conn.cursor() as cur:
        for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
            emb_str = str(emb) if emb else None
            cur.execute(
                """
                INSERT INTO chunks
                    (document_id, chunk_index, content, content_hash, token_count, embedding)
                VALUES (%s, %s, %s, %s, %s, %s::vector)
                ON CONFLICT DO NOTHING
                """,
                (document_id, i, chunk, sha256_text(chunk), len(chunk) // 4, emb_str),
            )


def set_status(conn, document_id: int, status: str,
               failure_reason: str | None = None,
               chunk_count: int | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE documents
               SET status = %s,
                   failure_reason = %s
             WHERE id = %s
            """,
            (status, failure_reason, document_id),
        )


def embed_chunks(chunks: list[str], pause_sec: float = 0.05) -> list[list[float] | None]:
    embeddings: list[list[float] | None] = []
    for c in chunks:
        embeddings.append(embed(c))
        if pause_sec:
            time.sleep(pause_sec)
    return embeddings
