import pytest
from retrieval.search import search, SearchFilters
from accounts.core import get_or_create_user, ensure_common_space, accessible_space_ids


def _mock_embed_request(monkeypatch, vector):
    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"embedding": vector}
    monkeypatch.setattr("ingestion.core.requests.post",
                        lambda *a, **k: FakeResp())


def _seed(db, doc_title, chunk_text, embedding, src="local_file", ext=".txt",
          space_id=None):
    if space_id is None:
        space_id = ensure_common_space(db)
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO documents
               (source_type, source_ref, title, content_hash, raw_content,
                status, file_extension, space_id, active)
               VALUES (%s, %s, %s, %s, '', 'indexed', %s, %s, TRUE) RETURNING id""",
            (src, f"/fake/{doc_title}", doc_title, f"h-{doc_title}", ext, space_id),
        )
        doc_id = cur.fetchone()["id"]
        cur.execute(
            """INSERT INTO chunks (document_id, chunk_index, content, content_hash, token_count, embedding)
               VALUES (%s, 0, %s, %s, %s, %s::vector)""",
            (doc_id, chunk_text, f"ch-{doc_title}", len(chunk_text) // 4, str(embedding)),
        )
    db.commit()


def _personal_space(db, user):
    common = ensure_common_space(db)
    return max(set(accessible_space_ids(db, user["id"])) - {common})


def test_search_returns_top_k(db, monkeypatch):
    _mock_embed_request(monkeypatch, [0.1] * 768)
    common = ensure_common_space(db)
    _seed(db, "match",   "churn prediction model", [0.11] * 768)
    _seed(db, "nomatch", "unrelated random text",  [0.99] * 768)
    results = search("churn", filters=SearchFilters(space_ids=[common]), top_k=5)
    assert any(r.document_title == "match" for r in results)


def test_search_filter_by_source(db, monkeypatch):
    _mock_embed_request(monkeypatch, [0.1] * 768)
    common = ensure_common_space(db)
    _seed(db, "from-local", "churn", [0.1] * 768, src="local_file")
    _seed(db, "from-web",   "churn", [0.1] * 768, src="web_ui")
    results = search("churn", filters=SearchFilters(source_types=["web_ui"],
                                                    space_ids=[common]), top_k=5)
    assert all(r.source_type == "web_ui" for r in results)


def test_search_scoped_to_space_ids(db, monkeypatch):
    _mock_embed_request(monkeypatch, [0.1] * 768)
    common = ensure_common_space(db)
    alice = get_or_create_user(db, "alice")
    alice_personal = _personal_space(db, alice)
    _seed(db, "in-common",  "churn", [0.1] * 768, space_id=common)
    _seed(db, "in-private", "churn", [0.1] * 768, space_id=alice_personal)
    out = search("churn", filters=SearchFilters(space_ids=[common]), top_k=5)
    titles = [r.document_title for r in out]
    assert "in-common" in titles
    assert "in-private" not in titles


def test_search_empty_space_ids_returns_nothing(db, monkeypatch):
    _mock_embed_request(monkeypatch, [0.1] * 768)
    _seed(db, "anything", "churn", [0.1] * 768)
    out = search("churn", filters=SearchFilters(space_ids=[]), top_k=5)
    assert out == []
