"""Direct unit tests for MCP tool functions (no MCP wire protocol)."""
from __future__ import annotations
import pytest

from accounts.core import (get_or_create_user, ensure_common_space,
                           accessible_space_ids)
import mcp_server.server as srv


def _mock_embed(monkeypatch, vec: list[float]):
    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"embedding": vec}
    monkeypatch.setattr("ingestion.core.requests.post",
                        lambda *a, **kw: FakeResp())


def _seed_doc(db, title, content, embedding, src="local_file", ext=".txt",
              status="indexed", space_id=None):
    if space_id is None:
        space_id = ensure_common_space(db)
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO documents
               (source_type, source_ref, title, content_hash, raw_content,
                status, file_extension, file_size, space_id, active)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE) RETURNING id""",
            (src, f"/fake/{title}", title, f"h-{title}", content,
             status, ext, len(content), space_id),
        )
        doc_id = cur.fetchone()["id"]
        cur.execute(
            """INSERT INTO chunks (document_id, chunk_index, content,
                                    content_hash, token_count, embedding)
               VALUES (%s,0,%s,%s,%s,%s::vector)""",
            (doc_id, content, f"ch-{title}", len(content)//4, str(embedding)),
        )
    db.commit()
    return doc_id


def _personal(db, user):
    common = ensure_common_space(db)
    return max(set(accessible_space_ids(db, user["id"])) - {common})


@pytest.fixture
def as_user(db, monkeypatch):
    u = get_or_create_user(db, "tooluser")
    db.commit()
    monkeypatch.setattr(srv, "_bearer_token", lambda: u["mcp_token"])
    return u


# ---------------------------------------------------------------------------
# _resolve_scope tests (Task 5, keep intact)
# ---------------------------------------------------------------------------

from mcp_server.server import _resolve_scope


def test_resolve_scope_valid_token(db):
    u = get_or_create_user(db, "mcpuser")
    db.commit()
    user, space_ids = _resolve_scope(u["mcp_token"])
    assert user["id"] == u["id"]
    assert set(space_ids) == set(accessible_space_ids(db, u["id"]))


def test_resolve_scope_invalid_token(db):
    user, space_ids = _resolve_scope("bogus")
    assert user is None and space_ids == []


# ---------------------------------------------------------------------------
# Tool tests — scoped to token user's spaces
# ---------------------------------------------------------------------------

def test_tool_search_requires_token(db, monkeypatch):
    monkeypatch.setattr(srv, "_bearer_token", lambda: None)
    _mock_embed(monkeypatch, [0.1] * 768)
    out = srv.search("churn", top_k=5)
    assert out == {"error": "unauthorized"}


def test_tool_search_scoped_to_user_spaces(db, monkeypatch, as_user):
    _mock_embed(monkeypatch, [0.1] * 768)
    common = ensure_common_space(db)
    other = get_or_create_user(db, "stranger"); db.commit()
    other_priv = _personal(db, other)
    _seed_doc(db, "mine",   "churn", [0.1]*768, space_id=common)
    _seed_doc(db, "theirs", "churn", [0.1]*768, space_id=other_priv)
    out = srv.search("churn", top_k=5)
    titles = [r["document_title"] for r in out]
    assert "mine" in titles and "theirs" not in titles


def test_tool_fetch_blocks_cross_space(db, as_user):
    other = get_or_create_user(db, "stranger2"); db.commit()
    other_priv = _personal(db, other)
    doc_id = _seed_doc(db, "secret", "x", [0.1]*768, space_id=other_priv)
    out = srv.fetch_document(doc_id)
    assert "error" in out


def test_tool_fetch_allows_accessible(db, as_user):
    common = ensure_common_space(db)
    doc_id = _seed_doc(db, "opendoc", "hello", [0.1]*768, space_id=common)
    out = srv.fetch_document(doc_id)
    assert out["id"] == doc_id and out["title"] == "opendoc"


def test_tool_list_documents_scoped(db, as_user):
    common = ensure_common_space(db)
    other = get_or_create_user(db, "stranger3"); db.commit()
    other_priv = _personal(db, other)
    _seed_doc(db, "visible", "x", [0.1]*768, space_id=common)
    _seed_doc(db, "hidden",  "x", [0.1]*768, space_id=other_priv)
    rows = srv.list_documents()
    titles = {r["title"] for r in rows}
    assert "visible" in titles and "hidden" not in titles


def test_tool_list_requires_token(db, monkeypatch):
    monkeypatch.setattr(srv, "_bearer_token", lambda: None)
    out = srv.list_documents()
    assert out == {"error": "unauthorized"}
