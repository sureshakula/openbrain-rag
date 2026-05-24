"""
VOZIQ Brain v0.3 — Review Queue Manager
----------------------------------------
Applies ACCEPT / REJECT / ABANDON decisions from the review queue.

ACCEPT  → Writes to knowledge_base. Document is now part of the RAG corpus.
REJECT  → Marks failed. Document goes back to 'chunked' for re-synthesis.
ABANDON → Marks abandoned. Document is excluded from future synthesis runs.

Usage:
    python review/review_queue.py --list                      # show pending items
    python review/review_queue.py --accept 7                  # accept queue entry 7
    python review/review_queue.py --reject 7 --note "too vague"
    python review/review_queue.py --abandon 7 --note "not relevant"
    python review/review_queue.py --stats                     # queue statistics
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent.parent))
from db.connection import get_conn


# ── Read ───────────────────────────────────────────────────────────────────────

def list_pending(conn, limit: int = 20):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                rq.id            AS queue_id,
                rq.status,
                rq.queued_at,
                d.id             AS doc_id,
                d.title,
                d.source_type,
                d.source_ref,
                LEFT(sr.synthesis_text, 300) AS preview
            FROM review_queue rq
            JOIN documents d        ON d.id = rq.document_id
            JOIN synthesis_runs sr  ON sr.id = rq.synthesis_run_id
            WHERE rq.status = 'PENDING'
            ORDER BY rq.queued_at
            LIMIT %s
            """,
            (limit,)
        )
        rows = cur.fetchall()

    if not rows:
        print("No pending items in review queue.")
        return

    print(f"\n{'─'*70}")
    print(f"  PENDING REVIEW QUEUE  ({len(rows)} items)")
    print(f"{'─'*70}")
    for r in rows:
        print(f"\n  Queue #{r['queue_id']}  |  Doc #{r['doc_id']}  |  {r['source_type']}")
        print(f"  Title   : {r['title'] or r['source_ref']}")
        print(f"  Queued  : {r['queued_at'].strftime('%Y-%m-%d %H:%M')}")
        print(f"  Preview : {r['preview']}...")
        print(f"{'─'*70}")


def stats(conn):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT status, COUNT(*) as count
            FROM review_queue
            GROUP BY status
            ORDER BY status
            """
        )
        rows = cur.fetchall()

        cur.execute("SELECT COUNT(*) AS total FROM knowledge_base")
        kb = cur.fetchone()

    print("\n  Review Queue Stats")
    print(f"  {'─'*30}")
    for r in rows:
        print(f"  {r['status']:<12} {r['count']:>5}")
    print(f"  {'─'*30}")
    print(f"  Knowledge base: {kb['total']} accepted entries\n")


# ── Write ──────────────────────────────────────────────────────────────────────

def _get_queue_entry(conn, queue_id: int) -> dict:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT rq.*, d.title, d.source_type, d.source_ref, d.metadata,
                   sr.synthesis_text
            FROM review_queue rq
            JOIN documents d        ON d.id = rq.document_id
            JOIN synthesis_runs sr  ON sr.id = rq.synthesis_run_id
            WHERE rq.id = %s
            """,
            (queue_id,)
        )
        row = cur.fetchone()
    if not row:
        raise ValueError(f"No review_queue entry with id={queue_id}")
    if row["status"] != "PENDING":
        raise ValueError(f"Entry {queue_id} is already {row['status']} — cannot re-review")
    return row


def accept(conn, queue_id: int, note: str = ""):
    entry = _get_queue_entry(conn, queue_id)

    with conn.cursor() as cur:
        # Write to knowledge base
        cur.execute(
            """
            INSERT INTO knowledge_base
                (review_queue_id, document_id, synthesis_run_id,
                 title, summary, source_type, source_ref, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                queue_id,
                entry["document_id"],
                entry["synthesis_run_id"],
                entry["title"],
                entry["synthesis_text"],
                entry["source_type"],
                entry["source_ref"],
                entry["metadata"],
            )
        )

        # Update queue status
        cur.execute(
            """
            UPDATE review_queue
            SET status='ACCEPTED', reviewer_note=%s, reviewed_at=NOW()
            WHERE id=%s
            """,
            (note, queue_id)
        )

    print(f"✓ Queue #{queue_id} ACCEPTED → added to knowledge_base")


def reject(conn, queue_id: int, note: str = ""):
    _get_queue_entry(conn, queue_id)

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE review_queue
            SET status='REJECTED', reviewer_note=%s, reviewed_at=NOW()
            WHERE id=%s
            """,
            (note, queue_id)
        )
        # Reset document to chunked so it can be re-synthesized
        cur.execute(
            """
            UPDATE documents SET status='chunked'
            WHERE id = (SELECT document_id FROM review_queue WHERE id=%s)
            """,
            (queue_id,)
        )

    print(f"✗ Queue #{queue_id} REJECTED — document reset to 'chunked' for re-synthesis")
    if note:
        print(f"  Note: {note}")


def abandon(conn, queue_id: int, note: str = ""):
    _get_queue_entry(conn, queue_id)

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE review_queue
            SET status='ABANDONED', reviewer_note=%s, reviewed_at=NOW()
            WHERE id=%s
            """,
            (note, queue_id)
        )
        cur.execute(
            """
            UPDATE documents SET status='failed'
            WHERE id = (SELECT document_id FROM review_queue WHERE id=%s)
            """,
            (queue_id,)
        )

    print(f"⊘ Queue #{queue_id} ABANDONED — document excluded from future synthesis")
    if note:
        print(f"  Note: {note}")


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VOZIQ Brain — Review Queue")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list",    action="store_true", help="List pending items")
    group.add_argument("--stats",   action="store_true", help="Queue statistics")
    group.add_argument("--accept",  type=int, metavar="QUEUE_ID")
    group.add_argument("--reject",  type=int, metavar="QUEUE_ID")
    group.add_argument("--abandon", type=int, metavar="QUEUE_ID")
    parser.add_argument("--note", default="", help="Optional reviewer note")
    args = parser.parse_args()

    with get_conn() as conn:
        if args.list:
            list_pending(conn)
        elif args.stats:
            stats(conn)
        elif args.accept is not None:
            accept(conn, args.accept, args.note)
        elif args.reject is not None:
            reject(conn, args.reject, args.note)
        elif args.abandon is not None:
            abandon(conn, args.abandon, args.note)
