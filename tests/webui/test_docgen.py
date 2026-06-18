import pytest
import respx
import httpx
from httpx import AsyncClient, ASGITransport

from webui.app import create_app
from accounts.core import ensure_common_space


@pytest.fixture
async def client():
    app = create_app(start_workers=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.cookies.set("ob_user", "dg")
        yield c


def _seed_chunk(db, title: str, content: str, embedding: list[float] | None = None):
    emb = embedding if embedding is not None else [0.1] * 768
    space_id = ensure_common_space(db)
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO documents (source_type, source_ref, title, content_hash, raw_content,
                                       status, file_extension, space_id, active)
               VALUES ('local_file', %s, %s, %s, '', 'indexed', '.md', %s, TRUE) RETURNING id""",
            (f"/fake/{title}", title, f"h-{title}", space_id),
        )
        doc_id = cur.fetchone()["id"]
        cur.execute(
            """INSERT INTO chunks (document_id, chunk_index, content, content_hash, token_count, embedding)
               VALUES (%s, 0, %s, %s, %s, %s::vector)""",
            (doc_id, content, f"ch-{title}", len(content) // 4, str(emb)),
        )
    db.commit()


async def test_get_docgen_form(client):
    r = await client.get("/docgen")
    assert r.status_code == 200
    assert "<form" in r.text
    assert 'name="topic"' in r.text
    assert "architecture" in r.text


@respx.mock
async def test_post_generate_creates_draft(client, db, monkeypatch):
    # Mock Ollama (requests) and Claude (httpx via respx)
    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"embedding": [0.1] * 768}
    monkeypatch.setattr("ingestion.core.requests.post",
                        lambda *a, **kw: FakeResp())
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(200, json={
            "content": [{"type": "text", "text": "Draft body referencing [1]."}],
            "model": "x", "usage": {"input_tokens": 1, "output_tokens": 1},
        })
    )
    _seed_chunk(db, "spec", "this is the body about churn")

    r = await client.post("/docgen/generate", data={
        "topic": "churn", "doc_type": "architecture",
    })
    assert r.status_code == 200
    assert "Draft body" in r.text
    assert "[1]" in r.text

    with db.cursor() as cur:
        cur.execute("SELECT topic, doc_type, status FROM drafts")
        row = cur.fetchone()
        assert row["topic"] == "churn"
        assert row["doc_type"] == "architecture"
        assert row["status"] == "pending"
        cur.execute("SELECT COUNT(*) AS n FROM draft_citations")
        assert cur.fetchone()["n"] >= 1
