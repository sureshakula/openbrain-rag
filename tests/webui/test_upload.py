import asyncio
import pytest
from pathlib import Path
from httpx import AsyncClient, ASGITransport

from webui.app import create_app


@pytest.fixture
async def client(tmp_upload_dir, monkeypatch):
    monkeypatch.setenv("VB_UPLOAD_DIR", str(tmp_upload_dir))
    import importlib, config as _config
    importlib.reload(_config)
    app = create_app(start_workers=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def authed_client(tmp_upload_dir, monkeypatch):
    """Client with ob_user cookie set to 'upl'."""
    monkeypatch.setenv("VB_UPLOAD_DIR", str(tmp_upload_dir))
    import importlib, config as _config
    importlib.reload(_config)
    app = create_app(start_workers=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.cookies.set("ob_user", "upl")
        yield c


async def test_get_upload_form(client):
    r = await client.get("/upload")
    assert r.status_code in (200, 307)


async def test_post_upload_creates_document_row(authed_client, db, tmp_upload_dir):
    from accounts.core import get_or_create_user, ensure_common_space
    user = get_or_create_user(db, "upl")
    db.commit()
    common = ensure_common_space(db)
    db.commit()

    payload_text = "tiny doc body"
    files = {"file": ("hello.txt", payload_text.encode("utf-8"), "text/plain")}
    r = await authed_client.post("/upload", data={"space": str(common)}, files=files)
    assert r.status_code in (200, 303)
    with db.cursor() as cur:
        cur.execute("SELECT id, status, source_type, file_extension, space_id, created_by FROM documents")
        row = cur.fetchone()
        assert row is not None
        assert row["status"] == "queued"
        assert row["source_type"] == "web_ui"
        assert row["file_extension"] == ".txt"
        assert row["space_id"] == common
        assert row["created_by"] == user["id"]
    files_on_disk = list(tmp_upload_dir.iterdir())
    assert len(files_on_disk) == 1


async def test_upload_assigns_space_and_creator(authed_client, db, tmp_upload_dir):
    from accounts.core import get_or_create_user, ensure_common_space
    user = get_or_create_user(db, "upl")
    db.commit()
    common = ensure_common_space(db)
    db.commit()

    files = {"file": ("n.md", b"# hi", "text/markdown")}
    r = await authed_client.post("/upload", data={"space": str(common)}, files=files)
    assert r.status_code in (200, 303)
    with db.cursor() as cur:
        cur.execute(
            "SELECT space_id, created_by FROM documents WHERE source_ref LIKE %s OR title=%s",
            ("%n.md", "n"),
        )
        row = cur.fetchone()
    assert row is not None
    assert row["space_id"] == common
    assert row["created_by"] == user["id"]


async def test_post_upload_rejects_oversize(authed_client, db, monkeypatch):
    from accounts.core import get_or_create_user, ensure_common_space
    get_or_create_user(db, "upl")
    db.commit()
    ensure_common_space(db)
    db.commit()

    monkeypatch.setenv("VB_MAX_UPLOAD_MB", "0")
    import importlib, config as _config
    importlib.reload(_config)
    huge = b"x" * (1024 * 1024 + 1)
    files = {"file": ("big.txt", huge, "text/plain")}
    r = await authed_client.post("/upload", files=files)
    assert r.status_code == 413


async def test_upload_without_auth_redirects(client, db):
    """Unauthenticated upload should redirect to /login."""
    files = {"file": ("x.txt", b"hello", "text/plain")}
    r = await client.post("/upload", files=files)
    assert r.status_code == 307
