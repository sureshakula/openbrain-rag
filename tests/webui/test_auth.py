import pytest
from httpx import AsyncClient, ASGITransport
from webui.app import create_app


@pytest.fixture
async def client():
    app = create_app(start_workers=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_inventory_without_cookie_redirects_to_login(client, db):
    r = await client.get("/inventory", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert "/login" in r.headers["location"]


async def test_login_sets_cookie_and_creates_user(client, db):
    r = await client.post("/login", data={"username": "dora"}, follow_redirects=False)
    assert r.status_code in (302, 303)
    assert "ob_user" in r.headers.get("set-cookie", "")
    with db.cursor() as cur:
        cur.execute("SELECT 1 FROM users WHERE username='dora'")
        assert cur.fetchone() is not None


async def test_login_page_renders(client, db):
    r = await client.get("/login")
    assert r.status_code == 200
    assert "username" in r.text.lower()
