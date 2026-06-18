import asyncio
import pytest
from pathlib import Path

from webui.workers.ingest_queue import ingest_one, IngestQueue
from ingestion.core import sha256_text
from accounts.core import ensure_common_space


@pytest.fixture
def sample_txt(tmp_path: Path) -> Path:
    p = tmp_path / "sample.txt"
    p.write_text("This is a short test document.\n\nWith two paragraphs.")
    return p


async def test_ingest_one_happy_path(db, sample_txt, monkeypatch):
    class FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    def fake_post(url, json=None, timeout=None, **kw):
        return FakeResp({"embedding": [0.1] * 768})

    monkeypatch.setattr("ingestion.core.requests.post", fake_post)

    space_id = ensure_common_space(db)
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO documents
               (source_type, source_ref, title, content_hash, raw_content,
                status, file_size, file_extension, space_id)
               VALUES ('web_ui', %s, 'sample', %s, '', 'queued', %s, '.txt', %s)
               RETURNING id""",
            (str(sample_txt), sha256_text(sample_txt.read_text()),
             sample_txt.stat().st_size, space_id),
        )
        doc_id = cur.fetchone()["id"]
    db.commit()

    await ingest_one(doc_id, sample_txt)

    with db.cursor() as cur:
        cur.execute("SELECT status, failure_reason FROM documents WHERE id=%s", (doc_id,))
        row = cur.fetchone()
        assert row["status"] == "indexed"
        assert row["failure_reason"] is None
        cur.execute("SELECT COUNT(*) AS n FROM chunks WHERE document_id=%s", (doc_id,))
        assert cur.fetchone()["n"] >= 1


async def test_ingest_one_unsupported_marks_failed(db, tmp_path):
    bad = tmp_path / "x.xyz"
    bad.write_bytes(b"junk")
    space_id = ensure_common_space(db)
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO documents
               (source_type, source_ref, title, content_hash, raw_content,
                status, file_size, file_extension, space_id)
               VALUES ('web_ui', %s, 'x', 'h1', '', 'queued', 4, '.xyz', %s) RETURNING id""",
            (str(bad), space_id),
        )
        doc_id = cur.fetchone()["id"]
    db.commit()

    await ingest_one(doc_id, bad)

    with db.cursor() as cur:
        cur.execute("SELECT status, failure_reason FROM documents WHERE id=%s", (doc_id,))
        row = cur.fetchone()
        assert row["status"] == "failed"
        assert "unsupported" in row["failure_reason"].lower()


async def test_ingest_queue_drains():
    q = IngestQueue()
    await q.start(worker_count=2)
    processed: list[int] = []

    async def fake_handler(doc_id: int, path: Path):
        processed.append(doc_id)

    q._handler = fake_handler  # type: ignore[assignment]
    for i in range(5):
        await q.enqueue(i, Path(f"/tmp/fake-{i}"))
    await q.join()
    await q.stop()
    assert sorted(processed) == [0, 1, 2, 3, 4]
