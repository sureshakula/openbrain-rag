"""Tests for FolderWatcher space-based ingestion (Task 12).

The watcher must ingest files into the space named by VB_WATCH_SPACE
(default "Common") rather than deriving a namespace from subfolder names.
"""
from __future__ import annotations
import asyncio
import pytest
from pathlib import Path

from accounts.core import ensure_common_space
from ingestion.core import sha256_text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_queue_spy():
    """Return a (spy_queue, enqueued_list) pair.
    spy_queue.enqueue records (doc_id, path) without doing real work.
    """
    enqueued: list[tuple[int, Path]] = []

    class SpyQueue:
        async def enqueue(self, doc_id: int, path: Path) -> None:
            enqueued.append((doc_id, path))

    return SpyQueue(), enqueued


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_watcher_ingest_lands_in_common_space(db, tmp_path):
    """A file watched at the root of watch_dir ends up in the Common space."""
    from webui.workers.folder_watcher import FolderWatcher
    import config
    import importlib

    # Ensure WATCH_SPACE is "Common" (default)
    original = config.WATCH_SPACE
    config.WATCH_SPACE = "Common"
    try:
        spy_q, enqueued = _make_queue_spy()

        txt = tmp_path / "hello.txt"
        txt.write_text("Hello watcher world")

        watcher = FolderWatcher(watch_dir=tmp_path, queue=spy_q, debounce_sec=0)
        # Directly call the enqueue path (bypass watchdog observer)
        await watcher._maybe_enqueue(txt)

        assert len(enqueued) == 1, "expected exactly one document enqueued"
        doc_id, queued_path = enqueued[0]
        assert queued_path == txt

        common_space_id = ensure_common_space(db)
        db.commit()

        with db.cursor() as cur:
            cur.execute("SELECT space_id FROM documents WHERE id = %s", (doc_id,))
            row = cur.fetchone()

        assert row is not None, "document row not found"
        assert row["space_id"] == common_space_id, (
            f"expected space_id={common_space_id}, got {row['space_id']}"
        )
    finally:
        config.WATCH_SPACE = original


async def test_watcher_subfolder_file_still_lands_in_common_space(db, tmp_path):
    """A file in a subfolder must NOT derive a namespace — it lands in Common."""
    from webui.workers.folder_watcher import FolderWatcher
    import config

    original = config.WATCH_SPACE
    config.WATCH_SPACE = "Common"
    try:
        spy_q, enqueued = _make_queue_spy()

        subdir = tmp_path / "sales" / "q3"
        subdir.mkdir(parents=True)
        txt = subdir / "report.txt"
        txt.write_text("Sales Q3 report content here")

        watcher = FolderWatcher(watch_dir=tmp_path, queue=spy_q, debounce_sec=0)
        await watcher._maybe_enqueue(txt)

        assert len(enqueued) == 1

        common_space_id = ensure_common_space(db)
        db.commit()

        doc_id, _ = enqueued[0]
        with db.cursor() as cur:
            cur.execute("SELECT space_id FROM documents WHERE id = %s", (doc_id,))
            row = cur.fetchone()

        assert row is not None
        assert row["space_id"] == common_space_id, (
            f"subfolder file must NOT derive namespace; expected space_id={common_space_id}, "
            f"got {row['space_id']}"
        )
    finally:
        config.WATCH_SPACE = original


async def test_watcher_dedup_skips_existing_hash(db, tmp_path):
    """Files already in the DB (by hash) are silently skipped."""
    from webui.workers.folder_watcher import FolderWatcher
    import config

    original = config.WATCH_SPACE
    config.WATCH_SPACE = "Common"
    try:
        spy_q, enqueued = _make_queue_spy()

        txt = tmp_path / "dup.txt"
        content = "Duplicate content"
        txt.write_text(content)
        h = sha256_text(content)

        common_space_id = ensure_common_space(db)
        # Pre-insert a doc with the same hash
        with db.cursor() as cur:
            cur.execute(
                """INSERT INTO documents
                       (source_type, source_ref, title, content_hash, raw_content,
                        status, file_size, file_extension, space_id)
                   VALUES ('local_file', %s, 'dup', %s, '', 'indexed', %s, '.txt', %s)""",
                (str(txt), h, len(content), common_space_id),
            )
        db.commit()

        watcher = FolderWatcher(watch_dir=tmp_path, queue=spy_q, debounce_sec=0)
        await watcher._maybe_enqueue(txt)

        assert len(enqueued) == 0, "duplicate file should not be enqueued"
    finally:
        config.WATCH_SPACE = original


async def test_watcher_no_namespace_derivation_attribute(tmp_path):
    """FolderWatcher must not expose a _namespace_for method."""
    from webui.workers.folder_watcher import FolderWatcher

    class NullQueue:
        async def enqueue(self, *a): pass

    watcher = FolderWatcher(watch_dir=tmp_path, queue=NullQueue(), debounce_sec=0)
    assert not hasattr(watcher, "_namespace_for"), (
        "_namespace_for must be removed; namespace derivation is no longer supported"
    )
