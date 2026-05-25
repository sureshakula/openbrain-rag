import pytest
from httpx import AsyncClient, ASGITransport
from webui.app import create_app


@pytest.fixture
async def client():
    app = create_app(start_workers=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _seed_doc(db, *, title, status, src="local_file", ext=".txt"):
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO documents
               (source_type, source_ref, title, content_hash, raw_content,
                status, file_extension, active)
               VALUES (%s, %s, %s, %s, '', %s, %s, TRUE) RETURNING id""",
            (src, f"/fake/{title}", title, f"hash-{title}", status, ext),
        )
        return cur.fetchone()["id"]


async def test_get_inventory_lists_docs(client, db):
    _seed_doc(db, title="a", status="indexed")
    _seed_doc(db, title="b", status="failed")
    db.commit()
    r = await client.get("/inventory")
    assert r.status_code == 200
    assert "a" in r.text and "b" in r.text


async def test_inventory_rows_partial_returns_only_tbody(client, db):
    _seed_doc(db, title="only-row", status="indexed")
    db.commit()
    r = await client.get("/inventory/rows")
    assert r.status_code == 200
    assert "<table" not in r.text
    assert "only-row" in r.text


async def test_inventory_filter_by_status(client, db):
    _seed_doc(db, title="ind1", status="indexed")
    _seed_doc(db, title="fail1", status="failed")
    db.commit()
    r = await client.get("/inventory/rows?status=failed")
    assert "fail1" in r.text
    assert "ind1" not in r.text
