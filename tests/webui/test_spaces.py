import pytest
from httpx import AsyncClient, ASGITransport
from webui.app import create_app
from accounts.core import get_or_create_user, create_shared_space, accessible_space_ids, space_by_name


@pytest.fixture
async def client():
    app = create_app(start_workers=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.cookies.set("ob_user", "spuser")
        yield c


async def test_spaces_page_lists_common(client, db):
    r = await client.get("/spaces")
    assert r.status_code == 200
    assert "Common" in r.text


async def test_create_shared_space(client, db):
    r = await client.post("/spaces/create", data={"name": "Marketing"},
                          follow_redirects=False)
    assert r.status_code in (302, 303)
    with db.cursor() as cur:
        cur.execute("SELECT 1 FROM spaces WHERE kind='shared' AND lower(name)='marketing'")
        assert cur.fetchone() is not None


async def test_join_shared_space(client, db):
    owner = get_or_create_user(db, "owner1")
    create_shared_space(db, "Research", owner["id"])
    db.commit()
    r = await client.post("/spaces/join", data={"name": "Research"},
                          follow_redirects=False)
    assert r.status_code in (302, 303)
    user = get_or_create_user(db, "spuser")
    sp = space_by_name(db, "Research")
    assert sp["id"] in accessible_space_ids(db, user["id"])
