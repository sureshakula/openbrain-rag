"""
VOZIQ Brain v0.3 — Local Folder Ingestion
------------------------------------------
Walks a folder, hashes each file for dedup, chunks the content,
generates embeddings via Ollama, and writes to documents + chunks tables.

Supported file types: .txt, .md, .pdf (text extraction), .py, .sql

Usage:
    python ingestion/ingest_local.py --folder /path/to/docs
    python ingestion/ingest_local.py --folder /path/to/docs --dry-run
    python ingestion/ingest_local.py --file /path/to/one/file.md
"""

import argparse
import hashlib
import re
import sys
import time
from pathlib import Path
from typing import Generator

import psycopg2.extras
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import (
    CHUNK_SIZE, CHUNK_OVERLAP,
    OLLAMA_BASE_URL, EMBEDDING_MODEL
)
from db.connection import get_conn, sha256


SUPPORTED_EXTENSIONS = {".txt", ".md", ".py", ".sql", ".rst", ".csv"}


# ── File reading ───────────────────────────────────────────────────────────────

def read_file(path: Path) -> str | None:
    """Read a file to text. Returns None if unsupported or unreadable."""
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        print(f"  ⚠ Could not read {path.name}: {e}")
        return None


# ── Chunking ───────────────────────────────────────────────────────────────────

def chunk_text(text: str, chunk_size: int = CHUNK_SIZE,
               overlap: int = CHUNK_OVERLAP) -> list[str]:
    """
    Simple character-based chunking with overlap.
    Tries to split on paragraph boundaries first, then falls back to character windows.
    chunk_size and overlap are in approximate characters (4 chars ≈ 1 token).
    """
    char_size = chunk_size * 4
    char_overlap = overlap * 4

    # Split on double newlines (paragraph boundaries)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]

    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        if len(current) + len(para) + 2 <= char_size:
            current = (current + "\n\n" + para).strip()
        else:
            if current:
                chunks.append(current)
            # Para itself exceeds chunk size — hard split
            if len(para) > char_size:
                for i in range(0, len(para), char_size - char_overlap):
                    chunks.append(para[i:i + char_size])
            else:
                current = para

    if current:
        chunks.append(current)

    return chunks


# ── Embeddings ─────────────────────────────────────────────────────────────────

def embed(text: str) -> list[float] | None:
    """Call Ollama to get an embedding vector."""
    try:
        resp = requests.post(
            f"{OLLAMA_BASE_URL}/api/embeddings",
            json={"model": EMBEDDING_MODEL, "prompt": text},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["embedding"]
    except Exception as e:
        print(f"  ⚠ Embedding failed: {e}")
        return None


# ── DB writes ──────────────────────────────────────────────────────────────────

def document_exists(conn, content_hash: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM documents WHERE content_hash = %s",
            (content_hash,)
        )
        return cur.fetchone() is not None


def insert_document(conn, path: Path, content: str, content_hash: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO documents
                (source_type, source_ref, title, content_hash, raw_content, metadata, status)
            VALUES ('local_file', %s, %s, %s, %s, %s, 'pending')
            RETURNING id
            """,
            (
                str(path),
                path.stem,
                content_hash,
                content,
                psycopg2.extras.Json({"extension": path.suffix, "size_bytes": path.stat().st_size}),
            )
        )
        return cur.fetchone()[0]


def insert_chunks(conn, document_id: int, chunks: list[str],
                  embeddings: list[list[float] | None]):
    with conn.cursor() as cur:
        for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
            chunk_hash = sha256(chunk)
            token_count = len(chunk) // 4  # rough approximation

            # pgvector expects a list as a string like '[0.1, 0.2, ...]'
            emb_str = str(emb) if emb else None

            cur.execute(
                """
                INSERT INTO chunks
                    (document_id, chunk_index, content, content_hash, token_count, embedding)
                VALUES (%s, %s, %s, %s, %s, %s::vector)
                ON CONFLICT DO NOTHING
                """,
                (document_id, i, chunk, chunk_hash, token_count, emb_str)
            )


def mark_chunked(conn, document_id: int):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE documents SET status='chunked' WHERE id=%s",
            (document_id,)
        )


# ── Main ───────────────────────────────────────────────────────────────────────

def ingest_file(conn, path: Path, dry_run: bool = False) -> bool:
    """Ingest a single file. Returns True if processed, False if skipped."""
    content = read_file(path)
    if content is None:
        return False

    content_hash = sha256(content)

    if document_exists(conn, content_hash):
        print(f"  = {path.name} — already ingested (hash match), skipping")
        return False

    print(f"  + {path.name} ({len(content)} chars)")

    chunks = chunk_text(content)
    print(f"    Chunks: {len(chunks)}")

    if dry_run:
        print(f"    [DRY RUN] Would embed {len(chunks)} chunks and write to DB")
        return True

    # Embed each chunk
    embeddings: list[list[float] | None] = []
    for i, chunk in enumerate(chunks):
        emb = embed(chunk)
        embeddings.append(emb)
        if emb:
            print(f"    Embedded chunk {i+1}/{len(chunks)}", end="\r")
        time.sleep(0.05)  # don't hammer Ollama
    print(f"    Embedded {sum(1 for e in embeddings if e)}/{len(chunks)} chunks")

    doc_id = insert_document(conn, path, content, content_hash)
    insert_chunks(conn, doc_id, chunks, embeddings)
    mark_chunked(conn, doc_id)

    print(f"    ✓ document_id={doc_id} | status=chunked → ready for synthesis")
    return True


def ingest_folder(folder: Path, dry_run: bool = False):
    files = sorted(
        f for f in folder.rglob("*")
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
    )

    if not files:
        print(f"No supported files found in {folder}")
        return

    print(f"\nIngesting {len(files)} file(s) from {folder}\n{'─'*50}")

    processed = skipped = failed = 0

    with get_conn() as conn:
        for path in files:
            try:
                result = ingest_file(conn, path, dry_run=dry_run)
                if result:
                    processed += 1
                else:
                    skipped += 1
            except Exception as e:
                print(f"  ✗ {path.name}: {e}")
                failed += 1

    print(f"\n{'─'*50}")
    print(f"Done. Processed: {processed} | Skipped: {skipped} | Failed: {failed}")
    if processed and not dry_run:
        print("→ Run synthesize.py to generate synthesis candidates")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VOZIQ Brain — Local Folder Ingestion")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--folder", type=Path, help="Ingest all files in a folder (recursive)")
    group.add_argument("--file",   type=Path, help="Ingest a single file")
    parser.add_argument("--dry-run", action="store_true", help="No DB writes")
    args = parser.parse_args()

    if args.file:
        with get_conn() as conn:
            ingest_file(conn, args.file, dry_run=args.dry_run)
    else:
        ingest_folder(args.folder, dry_run=args.dry_run)
