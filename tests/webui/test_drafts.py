import pytest
from httpx import AsyncClient, ASGITransport
from webui.app import create_app


@pytest.fixture
async def client():
    app = create_app(start_workers=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _seed_draft(db, topic="t", status="pending") -> int:
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO drafts (topic, doc_type, body_markdown, status)
               VALUES (%s, 'architecture', 'Body [1].', %s) RETURNING id""",
            (topic, status),
        )
        return cur.fetchone()["id"]


async def test_drafts_list_shows_pending(client, db):
    _seed_draft(db, "alpha", "pending")
    _seed_draft(db, "beta",  "accepted")
    db.commit()
    r = await client.get("/drafts")
    assert r.status_code == 200
    assert "alpha" in r.text
    assert "beta" in r.text


async def test_draft_detail(client, db):
    did = _seed_draft(db, "alpha", "pending")
    db.commit()
    r = await client.get(f"/drafts/{did}")
    assert r.status_code == 200
    assert "alpha" in r.text or "Body" in r.text


async def test_accept_publishes_and_updates(client, db):
    did = _seed_draft(db, "alpha", "pending")
    db.commit()
    r = await client.post(f"/drafts/{did}/accept")
    assert r.status_code == 200
    with db.cursor() as cur:
        cur.execute("SELECT status, reviewed_at FROM drafts WHERE id=%s", (did,))
        row = cur.fetchone()
        assert row["status"] == "accepted"
        assert row["reviewed_at"] is not None


async def test_reject_requires_feedback(client, db):
    did = _seed_draft(db, "alpha", "pending")
    db.commit()
    r = await client.post(f"/drafts/{did}/reject", data={"feedback": "needs more detail"})
    assert r.status_code == 200
    with db.cursor() as cur:
        cur.execute("SELECT status, feedback FROM drafts WHERE id=%s", (did,))
        row = cur.fetchone()
        assert row["status"] == "rejected"
        assert row["feedback"] == "needs more detail"


async def test_abandon(client, db):
    did = _seed_draft(db, "alpha", "pending")
    db.commit()
    r = await client.post(f"/drafts/{did}/abandon")
    assert r.status_code == 200
    with db.cursor() as cur:
        cur.execute("SELECT status FROM drafts WHERE id=%s", (did,))
        assert cur.fetchone()["status"] == "abandoned"
