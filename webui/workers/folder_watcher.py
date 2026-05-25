"""Watch a host folder; new/renamed files get enqueued for ingestion.

Uses watchdog (filesystem events). Dedupes by sha256 — files identical to
something already in `documents` are skipped. On startup, scans existing
contents of the folder so files dropped while the app is down still get
picked up.

Does NOT handle file deletion (out of scope; tombstoning is a separate spec).
Does NOT re-ingest on modification with identical content (sha256 unchanged).
On modification with new content, a NEW documents row is created — the prior
row stays as-is for history.
"""
from __future__ import annotations
import asyncio
import logging
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from db.connection import get_conn
from ingestion.core import (
    DEFAULT_NAMESPACE, document_exists, insert_document, sha256_text,
)
from webui.workers.ingest_queue import IngestQueue

log = logging.getLogger("openbrain.watcher")

_SKIP_EXT = {".tmp", ".swp", ".swx", ".part", ".crdownload", ".lock"}


def _is_processable(path: Path) -> bool:
    if path.name.startswith("."):
        return False
    if path.suffix.lower() in _SKIP_EXT:
        return False
    try:
        return path.is_file()
    except OSError:
        return False


class _Handler(FileSystemEventHandler):
    """Bridges watchdog (threaded) → asyncio loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop,
                 schedule: callable) -> None:
        self._loop = loop
        self._schedule = schedule

    def _dispatch(self, src: str) -> None:
        path = Path(src)
        if not _is_processable(path):
            return
        # Thread-safe: hop into asyncio loop
        asyncio.run_coroutine_threadsafe(self._schedule(path), self._loop)

    def on_created(self, event):
        if not event.is_directory:
            self._dispatch(event.src_path)

    def on_moved(self, event):
        # File rename target: treat as created
        if not event.is_directory:
            self._dispatch(event.dest_path)

    def on_modified(self, event):
        # Editors often write via temp + rename (handled by on_moved),
        # but some write in place. Sha256 dedup makes this safe to fire.
        if not event.is_directory:
            self._dispatch(event.src_path)


class FolderWatcher:
    """Wraps a watchdog Observer + dedupe/enqueue logic."""

    def __init__(self, watch_dir: Path, queue: IngestQueue,
                 debounce_sec: int = 2) -> None:
        self.watch_dir = watch_dir
        self.queue = queue
        self.debounce_sec = debounce_sec
        self._observer: Observer | None = None
        self._inflight: set[str] = set()  # path strs being debounced

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        # Initial scan: pick up anything already there
        for p in self.watch_dir.rglob("*"):
            if _is_processable(p):
                await self._maybe_enqueue(p)
        handler = _Handler(loop, self._schedule_debounced)
        self._observer = Observer()
        self._observer.schedule(handler, str(self.watch_dir), recursive=True)
        self._observer.start()
        log.info("folder watcher started on %s", self.watch_dir)

    async def stop(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=3)
            self._observer = None

    async def _schedule_debounced(self, path: Path) -> None:
        key = str(path)
        if key in self._inflight:
            return
        self._inflight.add(key)
        try:
            await asyncio.sleep(self.debounce_sec)
            if not path.exists():
                return
            await self._maybe_enqueue(path)
        finally:
            self._inflight.discard(key)

    def _namespace_for(self, path: Path) -> str:
        """Derive namespace from path's first subfolder under watch_dir.

        watch_dir/foo.md           → 'general'
        watch_dir/code/foo.py      → 'code'
        watch_dir/sales/q3/x.pdf   → 'sales' (only first segment)
        """
        try:
            rel = path.relative_to(self.watch_dir)
        except ValueError:
            return DEFAULT_NAMESPACE
        parts = rel.parts
        if len(parts) <= 1:
            return DEFAULT_NAMESPACE
        return parts[0].strip().lower() or DEFAULT_NAMESPACE

    async def _maybe_enqueue(self, path: Path) -> None:
        loop = asyncio.get_running_loop()
        ns = self._namespace_for(path)
        doc_id = await loop.run_in_executor(
            None, self._sync_check_and_insert, path, ns
        )
        if doc_id is not None:
            await self.queue.enqueue(doc_id, path)
            log.info("watcher enqueued %s as doc %d (ns=%s)", path.name, doc_id, ns)

    @staticmethod
    def _sync_check_and_insert(path: Path, namespace: str) -> int | None:
        try:
            data = path.read_bytes()
        except OSError as e:
            log.warning("watcher could not read %s: %s", path, e)
            return None
        content_hash = sha256_text(data.decode("utf-8", errors="replace"))
        with get_conn() as conn:
            if document_exists(conn, content_hash) is not None:
                return None
            return insert_document(
                conn,
                source_type="local_file",
                source_ref=str(path),
                title=path.stem,
                raw_content="",
                content_hash=content_hash,
                file_size=len(data),
                file_extension=path.suffix.lower(),
                status="queued",
                namespace=namespace,
                metadata={"watcher": True, "namespace": namespace},
            )
