"""
VOZIQ Brain v0.3 — Synthesizer
------------------------------
For each document in status='chunked', pulls its chunks, calls Claude to produce
a concise synthesis, writes to synthesis_runs, and enqueues in review_queue.

Usage:
    python synthesis/synthesize.py                  # process all pending
    python synthesis/synthesize.py --doc-id 42      # single document
    python synthesis/synthesize.py --dry-run        # print prompts, no DB writes
"""

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import anthropic
import psycopg2.extras

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import ANTHROPIC_API_KEY, SYNTHESIS_MODEL, SYNTHESIS_MAX_TOKENS
from db.connection import get_conn, sha256


# ── Prompt templates per source type ──────────────────────────────────────────

PROMPTS: dict[str, str] = {
    "local_file": """You are a knowledge assistant. Below are chunks from a document.
Write a concise synthesis (3-5 paragraphs) that:
- Captures the key ideas, decisions, or facts
- Preserves any specific names, dates, numbers that matter
- Is written in clear prose — no bullet points
- Ends with a one-sentence "Key takeaway"

Document title: {title}
Source: {source_ref}

--- CHUNKS ---
{chunks}
--- END CHUNKS ---

Synthesis:""",

    "outlook_email": """You are a knowledge assistant. Below are chunks from an email thread.
Write a concise synthesis (2-4 paragraphs) that:
- Summarises what was discussed, decided, or actioned
- Captures who said what only when attribution matters
- Notes any open questions or follow-ups
- Ends with a one-sentence "Key takeaway"

Thread subject: {title}
Participants: {participants}

--- CHUNKS ---
{chunks}
--- END CHUNKS ---

Synthesis:""",

    "web_ui": """You are a knowledge assistant. Below are chunks from a manually submitted note.
Write a concise synthesis (2-4 paragraphs) that captures the core ideas clearly.
End with a one-sentence "Key takeaway".

Title: {title}

--- CHUNKS ---
{chunks}
--- END CHUNKS ---

Synthesis:""",
}

DEFAULT_PROMPT = PROMPTS["local_file"]


# ── Core functions ─────────────────────────────────────────────────────────────

def fetch_pending_documents(conn, doc_id: int | None) -> list[dict]:
    """Return documents ready for synthesis (status='chunked')."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if doc_id:
            cur.execute(
                "SELECT * FROM documents WHERE id = %s AND status IN ('chunked', 'pending')",
                (doc_id,)
            )
        else:
            cur.execute(
                "SELECT * FROM documents WHERE status = 'chunked' ORDER BY ingested_at"
            )
        return cur.fetchall()


def fetch_chunks_for_document(conn, document_id: int) -> list[dict]:
    """Return all chunks for a document, ordered by position."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT chunk_index, content, token_count
            FROM chunks
            WHERE document_id = %s
            ORDER BY chunk_index
            """,
            (document_id,)
        )
        return cur.fetchall()


def build_prompt(doc: dict, chunks: list[dict]) -> str:
    """Select the right prompt template and fill it."""
    source_type = doc["source_type"]
    template = PROMPTS.get(source_type, DEFAULT_PROMPT)

    chunk_text = "\n\n---\n\n".join(
        f"[Chunk {c['chunk_index']+1}]\n{c['content']}" for c in chunks
    )

    metadata = doc.get("metadata") or {}
    participants = ", ".join(metadata.get("participants", [])) or "unknown"

    return template.format(
        title=doc.get("title") or "Untitled",
        source_ref=doc.get("source_ref") or "",
        participants=participants,
        chunks=chunk_text,
    )


def call_claude(prompt: str) -> tuple[str, int, int]:
    """
    Call Claude API and return (synthesis_text, prompt_tokens, completion_tokens).
    Raises on API error.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=SYNTHESIS_MODEL,
        max_tokens=SYNTHESIS_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()
    return text, response.usage.input_tokens, response.usage.output_tokens


def write_synthesis(conn, doc: dict, synthesis_text: str,
                    prompt_tokens: int, completion_tokens: int) -> int:
    """Insert into synthesis_runs and return the new run id."""
    run_hash = sha256(synthesis_text)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO synthesis_runs
                (document_id, model, prompt_tokens, completion_tokens,
                 synthesis_text, synthesis_hash)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (doc["id"], SYNTHESIS_MODEL, prompt_tokens, completion_tokens,
             synthesis_text, run_hash)
        )
        return cur.fetchone()[0]


def enqueue_for_review(conn, doc: dict, synthesis_run_id: int) -> int:
    """Insert into review_queue with status PENDING, return queue entry id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO review_queue
                (synthesis_run_id, document_id, status, review_channel)
            VALUES (%s, %s, 'PENDING', 'outlook')
            RETURNING id
            """,
            (synthesis_run_id, doc["id"])
        )
        return cur.fetchone()[0]


def mark_document_synthesized(conn, doc_id: int):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE documents SET status = 'synthesized' WHERE id = %s",
            (doc_id,)
        )


def mark_document_failed(conn, doc_id: int):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE documents SET status = 'failed' WHERE id = %s",
            (doc_id,)
        )


# ── Main loop ──────────────────────────────────────────────────────────────────

def run(doc_id: int | None = None, dry_run: bool = False):
    with get_conn() as conn:
        docs = fetch_pending_documents(conn, doc_id)

        if not docs:
            print("No documents pending synthesis.")
            return

        print(f"Found {len(docs)} document(s) to synthesize.")

        for doc in docs:
            print(f"\n→ [{doc['id']}] {doc['title'] or doc['source_ref']} ({doc['source_type']})")

            chunks = fetch_chunks_for_document(conn, doc["id"])
            if not chunks:
                print(f"  ⚠ No chunks found — skipping. Did ingestion run?")
                continue

            print(f"  Chunks: {len(chunks)} | "
                  f"~{sum(c.get('token_count') or 0 for c in chunks)} tokens")

            prompt = build_prompt(doc, chunks)

            if dry_run:
                print("  [DRY RUN] Prompt preview (first 500 chars):")
                print("  " + prompt[:500].replace("\n", "\n  "))
                continue

            try:
                print("  Calling Claude...", end=" ", flush=True)
                synthesis_text, pt, ct = call_claude(prompt)
                print(f"done. ({pt} in / {ct} out tokens)")

                run_id = write_synthesis(conn, doc, synthesis_text, pt, ct)
                queue_id = enqueue_for_review(conn, doc, run_id)
                mark_document_synthesized(conn, doc["id"])

                print(f"  ✓ synthesis_run={run_id} | review_queue={queue_id} | status=PENDING")
                print(f"  Preview: {synthesis_text[:200]}...")

            except Exception as e:
                print(f"  ✗ Failed: {e}")
                mark_document_failed(conn, doc["id"])

            # Polite pause between API calls
            time.sleep(0.5)

    print("\nSynthesis run complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VOZIQ Brain — Synthesizer")
    parser.add_argument("--doc-id", type=int, help="Process a single document by ID")
    parser.add_argument("--dry-run", action="store_true", help="Print prompts only, no writes")
    args = parser.parse_args()

    run(doc_id=args.doc_id, dry_run=args.dry_run)
