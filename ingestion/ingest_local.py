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
    python ingestion/ingest_local.py --folder /path/to/docs --namespace code
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


# ── Reusable core ──────────────────────────────────────────────────────────────
from ingestion.core import (
    DEFAULT_NAMESPACE, chunk_text, embed, document_exists,
    insert_chunks, set_status, sha256_text,
)


# ── Document insert (CLI-specific: 'local_file', from filesystem) ──────────────

def insert_document(conn, path: Path, content: str, content_hash: str,
                    namespace: str = DEFAULT_NAMESPACE) -> int:
    from ingestion.core import insert_document as _insert
    return _insert(
        conn,
        source_type="local_file",
        source_ref=str(path),
        title=path.stem,
        raw_content=content,
        content_hash=content_hash,
        file_size=path.stat().st_size,
        file_extension=path.suffix,
        status="pending",
        namespace=namespace,
        metadata={"extension": path.suffix, "size_bytes": path.stat().st_size,
                  "namespace": namespace},
    )


def mark_chunked(conn, document_id: int) -> None:
    set_status(conn, document_id, "chunked")


# ── Main ───────────────────────────────────────────────────────────────────────

def ingest_file(conn, path: Path, dry_run: bool = False,
                namespace: str = DEFAULT_NAMESPACE) -> bool:
    """Ingest a single file. Returns True if processed, False if skipped."""
    content = read_file(path)
    if content is None:
        return False

    content_hash = sha256(content)

    if document_exists(conn, content_hash) is not None:
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

    doc_id = insert_document(conn, path, content, content_hash, namespace=namespace)
    insert_chunks(conn, doc_id, chunks, embeddings)
    mark_chunked(conn, doc_id)

    print(f"    ✓ document_id={doc_id} | status=chunked → ready for synthesis")
    return True


def ingest_folder(folder: Path, dry_run: bool = False,
                  namespace: str = DEFAULT_NAMESPACE):
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
                result = ingest_file(conn, path, dry_run=dry_run, namespace=namespace)
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
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE,
                        help=f"Namespace tag for ingested docs (default: {DEFAULT_NAMESPACE})")
    args = parser.parse_args()
    namespace = (args.namespace or DEFAULT_NAMESPACE).strip().lower()

    if args.file:
        with get_conn() as conn:
            ingest_file(conn, args.file, dry_run=args.dry_run, namespace=namespace)
    else:
        ingest_folder(args.folder, dry_run=args.dry_run, namespace=namespace)
