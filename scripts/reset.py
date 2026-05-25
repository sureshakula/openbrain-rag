#!/usr/bin/env python3
"""Reset OpenBrain state: truncate webui tables, optionally wipe upload/watch dirs.

Usage:
    python scripts/reset.py                          # interactive confirm
    python scripts/reset.py --yes                    # skip confirm
    python scripts/reset.py --yes --files            # also delete uploads/ + watch/ contents
    python scripts/reset.py --yes --db openbrain     # target a specific DB
    python scripts/reset.py --yes --keep knowledge_base  # don't truncate this table

Runs against the configured Postgres (config.DB_*). Use inside the app
container for the compose stack:
    docker compose exec app python scripts/reset.py --yes
"""
from __future__ import annotations
import argparse
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2
from config import (
    DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME,
    UPLOAD_DIR, WATCH_DIR,
)


# Order matters only for CASCADE-less truncation, but RESTART IDENTITY CASCADE
# handles FK ordering for us.
ALL_TABLES = [
    "draft_citations",
    "drafts",
    "review_queue",
    "synthesis_runs",
    "knowledge_base",
    "chunks",
    "documents",
]


def _connect(dbname: str):
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER,
        password=DB_PASSWORD, dbname=dbname,
    )


def _row_counts(conn) -> dict[str, int]:
    counts: dict[str, int] = {}
    with conn.cursor() as cur:
        for t in ALL_TABLES:
            try:
                cur.execute(f"SELECT COUNT(*) FROM {t}")
                counts[t] = cur.fetchone()[0]
            except psycopg2.errors.UndefinedTable:
                conn.rollback()
                counts[t] = -1
    return counts


def _truncate(conn, tables: list[str]) -> None:
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE;")
    conn.commit()


def _wipe_dir(path: Path) -> int:
    if not path.is_dir():
        return 0
    removed = 0
    for entry in path.iterdir():
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
        removed += 1
    return removed


def main() -> int:
    p = argparse.ArgumentParser(description="Reset OpenBrain DB + optionally file dirs.")
    p.add_argument("--yes", action="store_true", help="Skip interactive confirmation.")
    p.add_argument("--db", default=DB_NAME, help=f"Database name (default: {DB_NAME}).")
    p.add_argument("--keep", action="append", default=[],
                   help="Table to skip (repeat to skip multiple). Default: truncate all.")
    p.add_argument("--files", action="store_true",
                   help="Also delete contents of UPLOAD_DIR and WATCH_DIR.")
    p.add_argument("--dry-run", action="store_true", help="Show what would change; don't do it.")
    args = p.parse_args()

    tables = [t for t in ALL_TABLES if t not in args.keep]

    print(f"Target DB:      {DB_USER}@{DB_HOST}:{DB_PORT}/{args.db}")
    print(f"Tables:         {', '.join(tables)}")
    if args.keep:
        print(f"Keeping:        {', '.join(args.keep)}")
    if args.files:
        print(f"Wipe upload:    {UPLOAD_DIR}")
        print(f"Wipe watch:     {WATCH_DIR or '(unset)'}")

    try:
        conn = _connect(args.db)
    except psycopg2.OperationalError as e:
        print(f"\nDB connect failed: {e}", file=sys.stderr)
        return 2

    try:
        before = _row_counts(conn)
        print("\nRow counts before:")
        for t in ALL_TABLES:
            n = before[t]
            status = "(missing)" if n == -1 else f"{n}"
            print(f"  {t:20s} {status}")

        if args.dry_run:
            print("\n[dry-run] no changes made.")
            return 0

        if not args.yes:
            print()
            try:
                resp = input("Proceed? type YES to confirm: ")
            except (EOFError, KeyboardInterrupt):
                resp = ""
            if resp.strip() != "YES":
                print("Aborted.")
                return 1

        _truncate(conn, tables)
        after = _row_counts(conn)
        print("\nRow counts after:")
        for t in ALL_TABLES:
            n = after[t]
            status = "(missing)" if n == -1 else f"{n}"
            print(f"  {t:20s} {status}")

        if args.files:
            print()
            up = Path(UPLOAD_DIR)
            wt = Path(WATCH_DIR) if WATCH_DIR else None
            n_up = _wipe_dir(up)
            print(f"  uploads:  removed {n_up} entries from {up}")
            if wt:
                n_wt = _wipe_dir(wt)
                print(f"  watch:    removed {n_wt} entries from {wt}")

        print("\nReset complete.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
