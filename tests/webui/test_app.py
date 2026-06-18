import pytest
from httpx import AsyncClient, ASGITransport

from webui.app import create_app


@pytest.fixture
async def client():
    app = create_app(start_workers=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.cookies.set("ob_user", "appuser")
        yield c


async def test_root_redirects_to_inventory(client):
    r = await client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"].endswith("/inventory")


async def test_static_css_served(client):
    r = await client.get("/static/app.css")
    assert r.status_code == 200
    assert "text/css" in r.headers["content-type"]


async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_pending_drafts_badge_in_inventory(client, db):
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO drafts (topic, doc_type, body_markdown, status)
               VALUES ('t1', 'process', 'b', 'pending'),
                      ('t2', 'process', 'b', 'pending'),
                      ('t3', 'process', 'b', 'accepted')"""
        )
    db.commit()
    r = await client.get("/inventory")
    assert "badge" in r.text
    assert ">2<" in r.text
