import pytest
from retrieval.search import search, SearchFilters


def _seed(db, doc_title: str, chunk_text: str, embedding: list[float], src="local_file",
          ext=".txt", namespace="general"):
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO documents
               (source_type, source_ref, title, content_hash, raw_content,
                status, file_extension, namespace, active)
               VALUES (%s, %s, %s, %s, '', 'indexed', %s, %s, TRUE) RETURNING id""",
            (src, f"/fake/{doc_title}", doc_title, f"h-{doc_title}", ext, namespace),
        )
        doc_id = cur.fetchone()["id"]
        cur.execute(
            """INSERT INTO chunks (document_id, chunk_index, content, content_hash, token_count, embedding)
               VALUES (%s, 0, %s, %s, %s, %s::vector)""",
            (doc_id, chunk_text, f"ch-{doc_title}", len(chunk_text) // 4, str(embedding)),
        )
    db.commit()


def _mock_embed_request(monkeypatch, vector: list[float]):
    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"embedding": vector}
    def fake_post(url, json=None, timeout=None, **kw):
        return FakeResp()
    monkeypatch.setattr("ingestion.core.requests.post", fake_post)


def test_search_returns_top_k(db, monkeypatch):
    _mock_embed_request(monkeypatch, [0.1] * 768)
    _seed(db, "match",   "churn prediction model",  [0.11] * 768)
    _seed(db, "nomatch", "unrelated random text",   [0.99] * 768)

    results = search("churn", top_k=5)
    assert len(results) >= 1
    titles = [r.document_title for r in results]
    assert "match" in titles


def test_search_filter_by_source(db, monkeypatch):
    _mock_embed_request(monkeypatch, [0.1] * 768)
    _seed(db, "from-local",  "churn",   [0.1] * 768, src="local_file")
    _seed(db, "from-web",    "churn",   [0.1] * 768, src="web_ui")

    results = search("churn", filters=SearchFilters(source_types=["web_ui"]), top_k=5)
    assert all(r.source_type == "web_ui" for r in results)


def test_search_filter_by_namespace(db, monkeypatch):
    _mock_embed_request(monkeypatch, [0.1] * 768)
    _seed(db, "in-code", "churn", [0.1] * 768, namespace="code")
    _seed(db, "in-ops",  "churn", [0.1] * 768, namespace="operations")

    results = search("churn", filters=SearchFilters(namespaces=["code"]), top_k=5)
    titles = [r.document_title for r in results]
    assert "in-code" in titles
    assert "in-ops" not in titles
