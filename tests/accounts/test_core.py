import pytest
from accounts.core import (
    ensure_common_space, get_or_create_user, user_by_token,
    accessible_space_ids, create_shared_space, join_space_by_name,
    list_spaces_for,
)


def test_ensure_common_space_idempotent(db):
    a = ensure_common_space(db)
    b = ensure_common_space(db)
    assert a == b and isinstance(a, int)


def test_get_or_create_user_is_idempotent_and_sets_up_spaces(db):
    u1 = get_or_create_user(db, "Alice")
    u2 = get_or_create_user(db, "alice")  # normalized to same user
    assert u1["id"] == u2["id"]
    assert u1["username"] == "alice"
    assert u1["mcp_token"]
    ids = accessible_space_ids(db, u1["id"])
    common = ensure_common_space(db)
    assert common in ids
    assert len(ids) >= 2  # personal + Common


def test_user_by_token(db):
    u = get_or_create_user(db, "bob")
    assert user_by_token(db, u["mcp_token"])["id"] == u["id"]
    assert user_by_token(db, "nope") is None


def test_private_spaces_are_isolated(db):
    a = get_or_create_user(db, "amy")
    b = get_or_create_user(db, "ben")
    a_ids = set(accessible_space_ids(db, a["id"]))
    b_ids = set(accessible_space_ids(db, b["id"]))
    assert a_ids & b_ids == {ensure_common_space(db)}


def test_create_and_join_shared_space(db):
    a = get_or_create_user(db, "ann")
    b = get_or_create_user(db, "bea")
    sp = create_shared_space(db, "Engineering", a["id"])
    assert sp["id"] not in accessible_space_ids(db, b["id"])
    join_space_by_name(db, "engineering", b["id"])
    assert sp["id"] in accessible_space_ids(db, b["id"])


def test_create_shared_space_rejects_duplicate(db):
    a = get_or_create_user(db, "dan")
    create_shared_space(db, "Sales", a["id"])
    with pytest.raises(ValueError):
        create_shared_space(db, "sales", a["id"])


def test_list_spaces_for_returns_metadata(db):
    a = get_or_create_user(db, "liz")
    rows = list_spaces_for(db, a["id"])
    names = {r["name"].lower() for r in rows}
    assert "common" in names
    assert all({"id", "name", "kind", "role", "doc_count"} <= set(r) for r in rows)
