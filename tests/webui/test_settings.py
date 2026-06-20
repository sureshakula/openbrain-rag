import pytest
from httpx import AsyncClient, ASGITransport
from webui.app import create_app
import settings.core as sc


@pytest.fixture
async def client():
    app = create_app(start_workers=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.cookies.set("ob_user", "setuser")
        yield c


async def test_settings_requires_login(db):
    app = create_app(start_workers=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/settings", follow_redirects=False)
        assert r.status_code in (302, 307) and "/login" in r.headers["location"]


async def test_get_settings_renders(client, db):
    r = await client.get("/settings")
    assert r.status_code == 200
    assert "max_upload_mb" in r.text
    assert "watch_space" in r.text


async def test_post_saves_valid(client, db):
    r = await client.post("/settings", data={
        "watch_enabled": "true", "watch_subfolder": "incoming",
        "watch_debounce_sec": "3", "watch_space": "Common",
        "ingest_worker_count": "4", "max_upload_mb": "120",
    }, follow_redirects=False)
    assert r.status_code in (302, 303)
    assert sc.get_int(db, "max_upload_mb") == 120
    assert sc.get(db, "watch_subfolder") == "incoming"


async def test_post_rejects_bad_subfolder(client, db):
    r = await client.post("/settings", data={
        "watch_subfolder": "../etc", "watch_debounce_sec": "2",
        "watch_space": "Common", "ingest_worker_count": "2", "max_upload_mb": "500",
    })
    assert r.status_code == 200
    assert "invalid" in r.text.lower()
    with db.cursor() as cur:
        cur.execute("SELECT 1 FROM settings WHERE key='watch_subfolder'")
        assert cur.fetchone() is None


async def test_post_rejects_negative_int(client, db):
    r = await client.post("/settings", data={
        "watch_subfolder": "", "watch_debounce_sec": "2",
        "watch_space": "Common", "ingest_worker_count": "0", "max_upload_mb": "500",
    })
    assert r.status_code == 200
    assert "invalid" in r.text.lower()
