"""Async ingestion queue: per-process asyncio worker pool that processes
uploaded files. Items are (document_id, file_path) tuples."""
from __future__ import annotations
import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable

from db.connection import get_conn
from ingestion.core import (
    chunk_text, embed_chunks, insert_chunks, set_status, sha256_text,
)
from webui.workers.file_dispatch import (
    extract_text, UnsupportedFileType, NotYetImplemented,
)

log = logging.getLogger("openbrain.ingest")


async def ingest_one(document_id: int, file_path: Path) -> None:
    """Process one document end-to-end. Runs blocking IO in a worker thread."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _ingest_one_sync, document_id, file_path)


def _ingest_one_sync(document_id: int, file_path: Path) -> None:
    try:
        with get_conn() as conn:
            set_status(conn, document_id, "processing")
        text, _meta = extract_text(file_path)
        chunks = chunk_text(text)
        embeddings = embed_chunks(chunks)
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE documents SET raw_content = %s, content_hash = %s WHERE id = %s",
                    (text, sha256_text(text), document_id),
                )
            insert_chunks(conn, document_id, chunks, embeddings)
            set_status(conn, document_id, "indexed")
    except UnsupportedFileType as e:
        _mark_failed(document_id, str(e))
    except NotYetImplemented as e:
        _mark_failed(document_id, str(e))
    except Exception as e:  # noqa: BLE001 - surface all failures to UI
        log.exception("ingest failed for doc %s", document_id)
        _mark_failed(document_id, f"{type(e).__name__}: {e}")


def _mark_failed(document_id: int, reason: str) -> None:
    with get_conn() as conn:
        set_status(conn, document_id, "failed", failure_reason=reason[:500])


Handler = Callable[[int, Path], Awaitable[None]]


class IngestQueue:
    """asyncio.Queue + worker tasks. Started by FastAPI lifespan."""

    def __init__(self) -> None:
        self._q: asyncio.Queue[tuple[int, Path]] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._handler: Handler = ingest_one

    async def start(self, worker_count: int) -> None:
        for _ in range(worker_count):
            self._workers.append(asyncio.create_task(self._run()))

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def enqueue(self, document_id: int, file_path: Path) -> None:
        await self._q.put((document_id, file_path))

    async def join(self) -> None:
        await self._q.join()

    async def _run(self) -> None:
        while True:
            doc_id, path = await self._q.get()
            try:
                await self._handler(doc_id, path)
            finally:
                self._q.task_done()
