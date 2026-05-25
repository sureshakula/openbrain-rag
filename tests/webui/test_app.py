import pytest
from httpx import AsyncClient, ASGITransport

from webui.app import create_app


@pytest.fixture
async def client():
    app = create_app(start_workers=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
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
