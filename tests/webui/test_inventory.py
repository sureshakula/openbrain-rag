import pytest
from httpx import AsyncClient, ASGITransport
from webui.app import create_app
from accounts.core import get_or_create_user, ensure_common_space, accessible_space_ids


@pytest.fixture
async def client():
    app = create_app(start_workers=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.cookies.set("ob_user", "invuser")
        yield c


def _seed_doc(db, *, title, status, src="local_file", ext=".txt", space_id=None):
    if space_id is None:
        space_id = ensure_common_space(db)
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO documents
               (source_type, source_ref, title, content_hash, raw_content,
                status, file_extension, space_id, active)
               VALUES (%s,%s,%s,%s,'',%s,%s,%s,TRUE) RETURNING id""",
            (src, f"/fake/{title}", title, f"hash-{title}", status, ext, space_id),
        )
        return cur.fetchone()["id"]


def _personal(db, user):
    common = ensure_common_space(db)
    return max(set(accessible_space_ids(db, user["id"])) - {common})


async def test_inventory_lists_accessible_docs(client, db):
    common = ensure_common_space(db)
    _seed_doc(db, title="visible", status="indexed", space_id=common)
    other = get_or_create_user(db, "other"); db.commit()
    other_priv = _personal(db, other)
    _seed_doc(db, title="hidden", status="indexed", space_id=other_priv)
    db.commit()
    r = await client.get("/inventory")
    assert r.status_code == 200
    assert "visible" in r.text
    assert "hidden" not in r.text


async def test_inventory_rows_partial_returns_only_tbody(client, db):
    common = ensure_common_space(db)
    _seed_doc(db, title="only-row", status="indexed", space_id=common)
    db.commit()
    r = await client.get("/inventory/rows")
    assert r.status_code == 200
    assert "<table" not in r.text
    assert "only-row" in r.text


async def test_inventory_space_filter(client, db):
    common = ensure_common_space(db)
    user = get_or_create_user(db, "invuser"); db.commit()
    priv = _personal(db, user)
    _seed_doc(db, title="c-doc", status="indexed", space_id=common)
    _seed_doc(db, title="p-doc", status="indexed", space_id=priv)
    db.commit()
    r = await client.get(f"/inventory/rows?space={priv}")
    assert "p-doc" in r.text and "c-doc" not in r.text
