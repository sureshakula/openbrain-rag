import pytest
import respx
import httpx
from httpx import AsyncClient, ASGITransport
from webui.app import create_app
from accounts.core import get_or_create_user, ensure_common_space, accessible_space_ids


@pytest.fixture
async def client():
    app = create_app(start_workers=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.cookies.set("ob_user", "chatuser")
        yield c


def _seed_chunk(db, title, content, space_id):
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO documents (source_type, source_ref, title, content_hash,
                                       raw_content, status, file_extension, space_id, active)
               VALUES ('local_file', %s, %s, %s, '', 'indexed', '.md', %s, TRUE) RETURNING id""",
            (f"/fake/{title}", title, f"h-{title}", space_id),
        )
        doc_id = cur.fetchone()["id"]
        cur.execute(
            """INSERT INTO chunks (document_id, chunk_index, content, content_hash, token_count, embedding)
               VALUES (%s, 0, %s, %s, %s, %s::vector)""",
            (doc_id, content, f"ch-{title}", 3, str([0.1] * 768)),
        )
    db.commit()


async def test_chat_requires_login(db):
    app = create_app(start_workers=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/chat", follow_redirects=False)
        assert r.status_code in (302, 307) and "/login" in r.headers["location"]


async def test_new_conversation_then_message(client, db, monkeypatch):
    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"embedding": [0.1] * 768}
    monkeypatch.setattr("ingestion.core.requests.post", lambda *a, **k: FakeResp())
    common = ensure_common_space(db)
    _seed_chunk(db, "churn", "churn is when customers leave", common)
    import chat.answer as ca
    monkeypatch.setattr(ca, "call_claude", lambda system, user: "Customers leaving [1].")

    r = await client.post("/chat/new", follow_redirects=False)
    assert r.status_code in (302, 303)
    conv_id = int(r.headers["location"].rstrip("/").split("/")[-1])

    r = await client.post(f"/chat/{conv_id}/message", data={"question": "what is churn?"})
    assert r.status_code == 200
    assert "Customers leaving" in r.text

    with db.cursor() as cur:
        cur.execute("SELECT role FROM messages WHERE conversation_id=%s ORDER BY id", (conv_id,))
        rows = cur.fetchall()
    assert [x["role"] for x in rows] == ["user", "assistant"]


async def test_cannot_access_other_users_conversation(client, db):
    other = get_or_create_user(db, "intruder-victim")
    from chat.core import create_conversation
    conv = create_conversation(db, other["id"]); db.commit()
    r = await client.get(f"/chat/{conv}", follow_redirects=False)
    assert r.status_code == 404


async def test_conversation_list_scoped(client, db):
    me = get_or_create_user(db, "chatuser")
    other = get_or_create_user(db, "someone-else")
    from chat.core import create_conversation
    create_conversation(db, me["id"], title="MINE-XYZ")
    create_conversation(db, other["id"], title="THEIRS-XYZ")
    db.commit()
    r = await client.get("/chat")
    assert "MINE-XYZ" in r.text and "THEIRS-XYZ" not in r.text
