"""Direct unit tests for MCP tool functions (no MCP wire protocol)."""
from __future__ import annotations
import pytest

from mcp_server.server import (
    search as t_search,
    fetch_document as t_fetch,
    list_documents as t_list,
)


def _mock_embed(monkeypatch, vec: list[float]):
    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"embedding": vec}
    monkeypatch.setattr("ingestion.core.requests.post",
                        lambda *a, **kw: FakeResp())


def _seed_doc(db, title: str, content: str, embedding: list[float],
              src: str = "local_file", ext: str = ".txt",
              status: str = "indexed") -> int:
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO documents
               (source_type, source_ref, title, content_hash, raw_content,
                status, file_extension, file_size, active)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE) RETURNING id""",
            (src, f"/fake/{title}", title, f"h-{title}", content,
             status, ext, len(content)),
        )
        doc_id = cur.fetchone()["id"]
        cur.execute(
            """INSERT INTO chunks (document_id, chunk_index, content,
                                    content_hash, token_count, embedding)
               VALUES (%s, 0, %s, %s, %s, %s::vector)""",
            (doc_id, content, f"ch-{title}", len(content) // 4, str(embedding)),
        )
    db.commit()
    return doc_id


def test_tool_search_returns_chunk_dicts(db, monkeypatch):
    _mock_embed(monkeypatch, [0.1] * 768)
    _seed_doc(db, "alpha", "churn prediction with transcripts", [0.11] * 768)

    out = t_search("churn", top_k=5)
    assert isinstance(out, list)
    assert len(out) >= 1
    first = out[0]
    assert {"chunk_id", "document_id", "document_title", "source_type",
            "file_extension", "content", "rrf_score"} <= set(first)
    assert "alpha" in [c["document_title"] for c in out]


def test_tool_search_respects_source_filter(db, monkeypatch):
    _mock_embed(monkeypatch, [0.1] * 768)
    _seed_doc(db, "from-local", "churn", [0.1] * 768, src="local_file")
    _seed_doc(db, "from-web",   "churn", [0.1] * 768, src="web_ui")

    out = t_search("churn", source_types=["web_ui"], top_k=5)
    assert all(r["source_type"] == "web_ui" for r in out)


def test_tool_fetch_document_returns_full_doc(db):
    doc_id = _seed_doc(db, "fetched", "full body here", [0.1] * 768)
    out = t_fetch(doc_id)
    assert out["id"] == doc_id
    assert out["title"] == "fetched"
    assert out["raw_content"] == "full body here"
    assert out["chunk_count"] >= 1
    # ingested_at must serialize to ISO string (not datetime)
    assert isinstance(out["ingested_at"], str)


def test_tool_fetch_document_missing_returns_error(db):
    out = t_fetch(999999)
    assert "error" in out


def test_tool_list_documents_filters(db):
    _seed_doc(db, "i1", "x", [0.1] * 768, status="indexed", src="local_file")
    _seed_doc(db, "i2", "y", [0.1] * 768, status="indexed", src="web_ui")
    _seed_doc(db, "f1", "z", [0.1] * 768, status="failed",  src="local_file")

    all_rows = t_list()
    titles_all = {r["title"] for r in all_rows}
    assert {"i1", "i2", "f1"} <= titles_all

    only_indexed = t_list(status="indexed")
    assert all(r["status"] == "indexed" for r in only_indexed)

    only_web = t_list(source="web_ui")
    assert all(r["source_type"] == "web_ui" for r in only_web)


def test_tool_list_documents_limit(db):
    for i in range(5):
        _seed_doc(db, f"d{i}", f"body {i}", [0.1] * 768)
    rows = t_list(limit=3)
    assert len(rows) == 3
