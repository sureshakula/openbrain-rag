"""Full pipeline: upload → worker → inventory → generate → draft → accept."""
import asyncio
import pytest
import respx
import httpx
from pathlib import Path
from httpx import AsyncClient, ASGITransport

from webui.app import create_app


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("VB_UPLOAD_DIR", str(tmp_path))
    import importlib, config as _config
    importlib.reload(_config)
    app = create_app(start_workers=True)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            c.cookies.set("ob_user", "smoke")
            yield c


@respx.mock
async def test_full_pipeline(client, db, monkeypatch):
    # Mock Ollama via requests
    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"embedding": [0.2] * 768}
    monkeypatch.setattr("ingestion.core.requests.post",
                        lambda *a, **kw: FakeResp())
    # Mock Claude via httpx (respx)
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(200, json={
            "content": [{"type": "text", "text": "About churn [1]."}],
            "model": "x", "usage": {"input_tokens": 1, "output_tokens": 1},
        })
    )

    # 1. Upload
    r = await client.post("/upload", files={
        "file": ("notes.md", b"# Notes\n\nChurn details here.", "text/markdown")
    })
    assert r.status_code in (200, 303)

    # 2. Wait for worker to drain
    queue = client._transport.app.state.queue
    await asyncio.wait_for(queue.join(), timeout=15)

    # 3. Inventory shows indexed
    r = await client.get("/inventory")
    assert "indexed" in r.text
    assert "notes" in r.text

    # 4. Generate draft
    r = await client.post("/docgen/generate", data={"topic": "churn", "doc_type": "process"})
    assert r.status_code == 200
    assert "About churn" in r.text

    # 5. Accept the generated draft
    with db.cursor() as cur:
        cur.execute("SELECT id FROM drafts ORDER BY id DESC LIMIT 1")
        draft_id = cur.fetchone()["id"]
    r = await client.post(f"/drafts/{draft_id}/accept")
    assert r.status_code == 200

    with db.cursor() as cur:
        cur.execute("SELECT status FROM drafts WHERE id=%s", (draft_id,))
        assert cur.fetchone()["status"] == "accepted"
