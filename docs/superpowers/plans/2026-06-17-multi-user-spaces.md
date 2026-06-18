# Multi-User Spaces Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add honor-system multi-user isolation with named shared spaces and an auto-join Common space, unifying the existing `namespace` column into a space concept across webui, MCP, CLI, and the folder watcher.

**Architecture:** Three new tables (`users`, `spaces`, `space_members`). Documents move from a free-form `namespace` string to a required `space_id` FK plus a nullable `created_by`. A single `accounts/core.py` helper is the source of truth for user/space/membership logic and for resolving the set of accessible space ids; every retrieval/listing query filters `space_id = ANY(<accessible ids>)` (fail-closed). Enforcement is app-layer only; the schema is shaped so Postgres RLS can be added later.

**Tech Stack:** Python 3.11, FastAPI + Jinja + HTMX, Postgres 16 + pgvector, psycopg2, pytest, FastMCP. Tests run in the app container against the `openbrain_test` DB; `tests/conftest.py` applies `db/schema.sql` idempotently each session and truncates webui tables per test.

---

## Reference: existing conventions

- DB access: `from db.connection import get_conn` — context manager that commits on success, rolls back on error, returns a pooled `psycopg2` connection. `sha256(text)` helper lives there too.
- Tests use the `db` fixture (function-scoped, `RealDictCursor`) which truncates a fixed table list per test. Seed helpers do raw `INSERT`s and `db.commit()`.
- `webui/app.py` exposes `create_app(*, start_workers=False)` and `render(request, template, ctx)`.
- Config reads env vars with `VB_`-prefixed names in `config.py`.
- Document insert path: `ingestion/core.py:insert_document(conn, *, ..., namespace=..., metadata=...)`.

## File Structure

- **Create** `accounts/__init__.py` — package marker.
- **Create** `accounts/core.py` — user/space/membership helper (the access layer).
- **Create** `tests/accounts/__init__.py`, `tests/accounts/test_core.py`.
- **Modify** `db/schema.sql` — new tables, seed Common, documents migration (idempotent).
- **Modify** `tests/conftest.py` — add new tables to the per-test TRUNCATE list.
- **Modify** `ingestion/core.py` — `insert_document` takes `space_id` + `created_by` instead of `namespace`.
- **Modify** `retrieval/search.py` — `SearchFilters.space_ids`; fail-closed scoping.
- **Modify** `mcp_server/server.py` — bearer-token auth + space scoping.
- **Modify** `webui/routes/upload.py` — target space select; set `space_id` + `created_by`.
- **Modify** `webui/routes/inventory.py` — current-user dependency, scoping, space dropdown/column.
- **Create** `webui/routes/auth.py` — `/login`, current-user dependency.
- **Create** `webui/routes/spaces.py` — `/spaces` list/create/join.
- **Modify** `webui/app.py` — register `auth` + `spaces` routers.
- **Modify** templates: `inventory.html`, `_inventory_rows.html`, `upload.html`; **create** `login.html`, `spaces.html`.
- **Modify** `ingestion/ingest_local.py` — `--space`/`--user`.
- **Modify** `webui/workers/folder_watcher.py` — ingest into `VB_WATCH_SPACE`.
- **Modify** `config.py` — add `WATCH_SPACE`.
- **Modify** `README.md` — document multi-user spaces.

---

## Phase 1 — Schema, migration, access helper

### Task 1: New tables + Common seed in schema.sql

**Files:**
- Modify: `db/schema.sql` (after the documents/chunks section, before WEBUI BM25 block or at end — append a new "MULTI-USER SPACES" section)
- Modify: `tests/conftest.py` (TRUNCATE list)

- [ ] **Step 1: Add the spaces section to `db/schema.sql`**

Append (idempotent):

```sql
-- ─────────────────────────────────────────
-- MULTI-USER SPACES
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id          SERIAL PRIMARY KEY,
    username    TEXT NOT NULL UNIQUE,
    mcp_token   TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS spaces (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('personal','shared')),
    created_by  INTEGER REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- One shared space per name (case-insensitive). Personal names are not unique.
CREATE UNIQUE INDEX IF NOT EXISTS idx_spaces_shared_name
    ON spaces (lower(name)) WHERE kind = 'shared';

CREATE TABLE IF NOT EXISTS space_members (
    space_id    INTEGER NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL REFERENCES users(id)  ON DELETE CASCADE,
    role        TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('owner','member')),
    PRIMARY KEY (space_id, user_id)
);

-- Seed the built-in Common shared space (created_by NULL).
INSERT INTO spaces (name, kind, created_by)
SELECT 'Common', 'shared', NULL
WHERE NOT EXISTS (
    SELECT 1 FROM spaces WHERE kind = 'shared' AND lower(name) = 'common'
);
```

- [ ] **Step 2: Add new tables to the per-test TRUNCATE list in `tests/conftest.py`**

In the `db` fixture, change the TRUNCATE to include the new tables (so test isolation holds). Common is re-seeded on demand by the access helper, so truncating `spaces` is safe:

```python
        cur.execute("""
            TRUNCATE draft_citations, drafts, review_queue, synthesis_runs,
                     knowledge_base, chunks, documents,
                     space_members, spaces, users RESTART IDENTITY CASCADE;
        """)
```

- [ ] **Step 3: Verify schema loads cleanly**

Run (on u24): drop the test schema and reload via the suite bootstrap.

```bash
ssh u24 'cd ~/projects/openbrain-rag && docker exec openbrain-rag-db-1 psql -U postgres -d openbrain_test -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" && docker compose run --rm -e VB_DB_HOST=db -e VB_DB_PORT=5432 -e VB_DB_NAME=openbrain_test --entrypoint "" app python -c "import tests.conftest" 2>&1 | tail -3'
```

Expected: no error (import is a smoke check; real verification is Task 2's tests).

- [ ] **Step 4: Commit**

```bash
git add db/schema.sql tests/conftest.py
git commit -m "feat(spaces): users/spaces/space_members tables + Common seed"
```

### Task 2: Access helper `accounts/core.py`

**Files:**
- Create: `accounts/__init__.py` (empty)
- Create: `accounts/core.py`
- Create: `tests/accounts/__init__.py` (empty)
- Test: `tests/accounts/test_core.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/accounts/test_core.py`:

```python
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
    # personal space + Common membership exist
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
    # personal spaces differ; only Common overlaps
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/accounts/test_core.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'accounts'`.

- [ ] **Step 3: Create the package + implementation**

Create `accounts/__init__.py` (empty) and `tests/accounts/__init__.py` (empty).

Create `accounts/core.py`:

```python
"""Users, spaces, membership — the app-layer access control source of truth."""
from __future__ import annotations
import secrets


def _norm(name: str) -> str:
    return (name or "").strip().lower()


def ensure_common_space(conn) -> int:
    """Return the Common shared space id, creating it if missing."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM spaces WHERE kind='shared' AND lower(name)='common'"
        )
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute(
            "INSERT INTO spaces (name, kind, created_by) "
            "VALUES ('Common','shared',NULL) RETURNING id"
        )
        return cur.fetchone()[0]


def get_or_create_user(conn, username: str) -> dict:
    """Get-or-create a user by normalized username. Creates the user's personal
    space (owner membership) and joins Common. Returns {id, username, mcp_token}."""
    uname = _norm(username)
    if not uname:
        raise ValueError("username required")
    with conn.cursor() as cur:
        cur.execute("SELECT id, username, mcp_token FROM users WHERE username=%s",
                    (uname,))
        row = cur.fetchone()
        if row:
            return {"id": row[0], "username": row[1], "mcp_token": row[2]}
        token = secrets.token_urlsafe(24)
        cur.execute(
            "INSERT INTO users (username, mcp_token) VALUES (%s,%s) RETURNING id",
            (uname, token),
        )
        uid = cur.fetchone()[0]
        # personal space
        cur.execute(
            "INSERT INTO spaces (name, kind, created_by) VALUES (%s,'personal',%s) "
            "RETURNING id", (uname, uid),
        )
        personal_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO space_members (space_id, user_id, role) VALUES (%s,%s,'owner')",
            (personal_id, uid),
        )
    common_id = ensure_common_space(conn)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO space_members (space_id, user_id, role) VALUES (%s,%s,'member') "
            "ON CONFLICT DO NOTHING", (common_id, uid),
        )
    return {"id": uid, "username": uname, "mcp_token": token}


def user_by_token(conn, token: str) -> dict | None:
    if not token:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT id, username, mcp_token FROM users WHERE mcp_token=%s",
                    (token,))
        row = cur.fetchone()
        if not row:
            return None
        return {"id": row[0], "username": row[1], "mcp_token": row[2]}


def accessible_space_ids(conn, user_id: int) -> list[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT space_id FROM space_members WHERE user_id=%s", (user_id,))
        return [r[0] for r in cur.fetchall()]


def create_shared_space(conn, name: str, owner_user_id: int) -> dict:
    nname = _norm(name)
    if not nname:
        raise ValueError("space name required")
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM spaces WHERE kind='shared' AND lower(name)=%s",
                    (nname,))
        if cur.fetchone():
            raise ValueError(f"shared space '{name}' already exists")
        cur.execute(
            "INSERT INTO spaces (name, kind, created_by) VALUES (%s,'shared',%s) "
            "RETURNING id, name, kind", (name.strip(), owner_user_id),
        )
        sid, sname, skind = cur.fetchone()
        cur.execute(
            "INSERT INTO space_members (space_id, user_id, role) VALUES (%s,%s,'owner')",
            (sid, owner_user_id),
        )
        return {"id": sid, "name": sname, "kind": skind}


def space_by_name(conn, name: str) -> dict | None:
    """Look up a shared space by name (case-insensitive)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, kind FROM spaces WHERE kind='shared' AND lower(name)=%s",
            (_norm(name),),
        )
        row = cur.fetchone()
        return {"id": row[0], "name": row[1], "kind": row[2]} if row else None


def join_space_by_name(conn, name: str, user_id: int) -> dict:
    sp = space_by_name(conn, name)
    if not sp:
        raise ValueError(f"shared space '{name}' not found")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO space_members (space_id, user_id, role) VALUES (%s,%s,'member') "
            "ON CONFLICT DO NOTHING", (sp["id"], user_id),
        )
    return sp


def list_spaces_for(conn, user_id: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.id, s.name, s.kind, m.role,
                   (SELECT COUNT(*) FROM documents d WHERE d.space_id = s.id) AS doc_count
              FROM spaces s
              JOIN space_members m ON m.space_id = s.id AND m.user_id = %s
             ORDER BY (s.kind='shared' AND lower(s.name)='common') DESC,
                      s.kind, lower(s.name)
            """,
            (user_id,),
        )
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/accounts/test_core.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add accounts/ tests/accounts/
git commit -m "feat(accounts): user/space/membership access helper"
```

### Task 3: Documents migration — space_id/created_by, drop namespace

**Files:**
- Modify: `db/schema.sql` (documents section)
- Modify: `ingestion/core.py:insert_document`
- Test: `tests/ingestion/test_core.py` (add migration + insert test)

- [ ] **Step 1: Write the failing test**

Add to `tests/ingestion/test_core.py`:

```python
def test_insert_document_uses_space_and_creator(db):
    from accounts.core import get_or_create_user, ensure_common_space
    from ingestion.core import insert_document
    u = get_or_create_user(db, "carol")
    common = ensure_common_space(db)
    doc_id = insert_document(
        db, source_type="local_file", source_ref="/x.md", title="x",
        raw_content="body", content_hash="h-x", file_size=4, file_extension=".md",
        status="indexed", space_id=common, created_by=u["id"],
    )
    with db.cursor() as cur:
        cur.execute("SELECT space_id, created_by FROM documents WHERE id=%s", (doc_id,))
        space_id, created_by = cur.fetchone()
    assert space_id == common and created_by == u["id"]


def test_documents_has_no_namespace_column(db):
    with db.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='documents' AND column_name='namespace'"
        )
        assert cur.fetchone() is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/ingestion/test_core.py -k "space_and_creator or no_namespace" -v`
Expected: FAIL — `insert_document` has no `space_id`/`created_by` kwargs; namespace column still present.

- [ ] **Step 3: Update `db/schema.sql` documents migration**

In the `WEBUI: DOCUMENTS EXTENSIONS` block, remove the `namespace` column addition and its index (added in a prior commit) and replace the trailing migration with:

```sql
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS space_id   INTEGER REFERENCES spaces(id),
    ADD COLUMN IF NOT EXISTS created_by INTEGER REFERENCES users(id);

-- Backfill legacy rows into Common, preserving the old namespace value.
DO $$
DECLARE common_id INTEGER;
BEGIN
    SELECT id INTO common_id FROM spaces WHERE kind='shared' AND lower(name)='common';
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='documents' AND column_name='namespace') THEN
        EXECUTE
          'UPDATE documents
              SET metadata = jsonb_set(coalesce(metadata,''{}''::jsonb),
                                       ''{namespace_legacy}'', to_jsonb(namespace), true)
            WHERE namespace IS NOT NULL';
    END IF;
    UPDATE documents SET space_id = common_id WHERE space_id IS NULL;
END $$;

ALTER TABLE documents ALTER COLUMN space_id SET NOT NULL;
DROP INDEX IF EXISTS idx_documents_namespace;
ALTER TABLE documents DROP COLUMN IF EXISTS namespace;
CREATE INDEX IF NOT EXISTS idx_documents_space ON documents(space_id);
```

This block must appear **after** the spaces tables + Common seed in the file. Reorder if necessary so the spaces section precedes the documents migration.

- [ ] **Step 4: Update `ingestion/core.py:insert_document`**

Replace the `namespace` parameter with `space_id` (required) and `created_by` (nullable):

```python
def insert_document(conn, *, source_type: str, source_ref: str, title: str,
                    raw_content: str, content_hash: str, file_size: int | None,
                    file_extension: str | None, status: str,
                    space_id: int, created_by: int | None = None,
                    metadata: dict | None = None) -> int:
    """Insert a document row. Returns new document id."""
    metadata = metadata or {}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO documents
                (source_type, source_ref, title, content_hash, raw_content,
                 metadata, status, file_size, file_extension, space_id, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (source_type, source_ref, title, content_hash, raw_content,
             psycopg2.extras.Json(metadata), status, file_size, file_extension,
             space_id, created_by),
        )
        return cur.fetchone()[0]
```

Also delete the now-unused `DEFAULT_NAMESPACE` / `SUGGESTED_NAMESPACES` constants in `ingestion/core.py` (search the repo first; they are imported by `ingest_local.py`, `upload.py`, `folder_watcher.py` — those are updated in Phase 5, so do this constant removal in Task 10 to avoid breaking imports mid-phase). For now, leave the constants in place.

- [ ] **Step 5: Run to verify it passes**

Run (drop+reload test schema first so the migration runs):

```bash
ssh u24 'cd ~/projects/openbrain-rag && docker exec openbrain-rag-db-1 psql -U postgres -d openbrain_test -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" && docker compose run --rm -e VB_DB_HOST=db -e VB_DB_PORT=5432 -e VB_DB_NAME=openbrain_test --entrypoint "" app python -m pytest tests/ingestion/test_core.py -v 2>&1 | tail -15'
```

Expected: PASS, including the two new tests.

- [ ] **Step 6: Commit**

```bash
git add db/schema.sql ingestion/core.py tests/ingestion/test_core.py
git commit -m "feat(spaces): documents.space_id/created_by + migrate namespace to Common"
```

> NOTE: Other callers of `insert_document` (`ingest_local.py`, `upload.py`) still pass `namespace=` and will be red until Phase 5. Run the full suite only after Phase 5. Per-file targeted tests in Phases 2–4 stay green because they seed via the access helper and call `insert_document` with `space_id`.

---

## Phase 2 — Retrieval scoping

### Task 4: `SearchFilters.space_ids` + fail-closed scoping

**Files:**
- Modify: `retrieval/search.py`
- Test: `tests/retrieval/test_search.py`

- [ ] **Step 1: Rewrite the namespace test as a space-scoping test**

Replace `test_search_filter_by_namespace` in `tests/retrieval/test_search.py` and update the `_seed` helper. New `_seed` signature takes `space_id`; update the existing tests' `_seed` calls to pass a space. Add at the top of the test module a fixture-free helper:

```python
from retrieval.search import search, SearchFilters
from accounts.core import get_or_create_user, ensure_common_space


def _seed(db, doc_title, chunk_text, embedding, src="local_file", ext=".txt",
          space_id=None):
    if space_id is None:
        space_id = ensure_common_space(db)
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO documents
               (source_type, source_ref, title, content_hash, raw_content,
                status, file_extension, space_id, active)
               VALUES (%s, %s, %s, %s, '', 'indexed', %s, %s, TRUE) RETURNING id""",
            (src, f"/fake/{doc_title}", doc_title, f"h-{doc_title}", ext, space_id),
        )
        doc_id = cur.fetchone()["id"]
        cur.execute(
            """INSERT INTO chunks (document_id, chunk_index, content, content_hash, token_count, embedding)
               VALUES (%s, 0, %s, %s, %s, %s::vector)""",
            (doc_id, chunk_text, f"ch-{doc_title}", len(chunk_text) // 4, str(embedding)),
        )
    db.commit()
```

Replace the namespace test with:

```python
def test_search_scoped_to_space_ids(db, monkeypatch):
    _mock_embed_request(monkeypatch, [0.1] * 768)
    common = ensure_common_space(db)
    alice = get_or_create_user(db, "alice")
    alice_personal = max(set(__import__("accounts.core", fromlist=["accessible_space_ids"])
                              .accessible_space_ids(db, alice["id"])) - {common})
    _seed(db, "in-common",   "churn", [0.1] * 768, space_id=common)
    _seed(db, "in-private",  "churn", [0.1] * 768, space_id=alice_personal)

    out = search("churn", filters=SearchFilters(space_ids=[common]), top_k=5)
    titles = [r.document_title for r in out]
    assert "in-common" in titles
    assert "in-private" not in titles


def test_search_empty_space_ids_returns_nothing(db, monkeypatch):
    _mock_embed_request(monkeypatch, [0.1] * 768)
    _seed(db, "anything", "churn", [0.1] * 768)
    out = search("churn", filters=SearchFilters(space_ids=[]), top_k=5)
    assert out == []
```

Keep `test_search_returns_top_k` and `test_search_filter_by_source`, but update their `_seed` calls (they default to Common now) and wrap them so they pass `space_ids=[common]`:

```python
def test_search_returns_top_k(db, monkeypatch):
    _mock_embed_request(monkeypatch, [0.1] * 768)
    common = ensure_common_space(db)
    _seed(db, "match",   "churn prediction model", [0.11] * 768)
    _seed(db, "nomatch", "unrelated random text",  [0.99] * 768)
    results = search("churn", filters=SearchFilters(space_ids=[common]), top_k=5)
    assert any(r.document_title == "match" for r in results)
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/retrieval/test_search.py -v`
Expected: FAIL — `SearchFilters` has no `space_ids`.

- [ ] **Step 3: Implement scoping in `retrieval/search.py`**

Replace the `namespaces` field and its WHERE clause:

```python
@dataclass
class SearchFilters:
    source_types: list[str] = field(default_factory=list)
    file_extensions: list[str] = field(default_factory=list)
    space_ids: list[int] | None = None   # None = no scope filter; [] = fail-closed
```

In `_build_where`:

```python
def _build_where(filters: SearchFilters | None) -> tuple[str, list]:
    clauses = ["d.active = TRUE"]
    args: list = []
    if filters and filters.source_types:
        clauses.append("d.source_type = ANY(%s)"); args.append(filters.source_types)
    if filters and filters.file_extensions:
        clauses.append("d.file_extension = ANY(%s)"); args.append(filters.file_extensions)
    if filters is not None and filters.space_ids is not None:
        # explicit scope: empty list means "no accessible spaces" -> match nothing
        clauses.append("d.space_id = ANY(%s)"); args.append(filters.space_ids)
    return " AND ".join(clauses), args
```

Note: `space_ids=[]` produces `d.space_id = ANY('{}')`, which matches zero rows — the fail-closed behavior. `space_ids=None` (the default) applies no space filter, used only by internal/admin callers; webui and MCP always pass an explicit list.

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/retrieval/test_search.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add retrieval/search.py tests/retrieval/test_search.py
git commit -m "feat(search): scope retrieval by space_ids (fail-closed)"
```

---

## Phase 3 — MCP token auth + scoping

### Task 5: Bearer-token resolution helper for MCP

**Files:**
- Modify: `mcp_server/server.py`
- Test: `tests/mcp_server/test_tools.py`

- [ ] **Step 1: Write the failing tests**

Update `_seed_doc` in `tests/mcp_server/test_tools.py` to take `space_id` (default Common), and add token/scoping tests. Add imports:

```python
from accounts.core import get_or_create_user, ensure_common_space, accessible_space_ids
from mcp_server.server import _resolve_scope
```

New `_seed_doc` body inserts `space_id` instead of `namespace`:

```python
def _seed_doc(db, title, content, embedding, src="local_file", ext=".txt",
              status="indexed", space_id=None):
    if space_id is None:
        space_id = ensure_common_space(db)
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO documents
               (source_type, source_ref, title, content_hash, raw_content,
                status, file_extension, file_size, space_id, active)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE) RETURNING id""",
            (src, f"/fake/{title}", title, f"h-{title}", content,
             status, ext, len(content), space_id),
        )
        doc_id = cur.fetchone()["id"]
        cur.execute(
            """INSERT INTO chunks (document_id, chunk_index, content,
                                    content_hash, token_count, embedding)
               VALUES (%s,0,%s,%s,%s,%s::vector)""",
            (doc_id, content, f"ch-{title}", len(content)//4, str(embedding)),
        )
    db.commit()
    return doc_id


def test_resolve_scope_valid_token(db):
    u = get_or_create_user(db, "mcpuser")
    user, space_ids = _resolve_scope(u["mcp_token"])
    assert user["id"] == u["id"]
    assert set(space_ids) == set(accessible_space_ids(db, u["id"]))


def test_resolve_scope_invalid_token(db):
    user, space_ids = _resolve_scope("bogus")
    assert user is None and space_ids == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/mcp_server/test_tools.py -k resolve_scope -v`
Expected: FAIL — `_resolve_scope` does not exist.

- [ ] **Step 3: Implement `_resolve_scope` + header reading in `mcp_server/server.py`**

Add imports and helper near the top (after existing imports):

```python
from accounts.core import user_by_token, accessible_space_ids


def _bearer_token() -> str | None:
    """Read the Authorization: Bearer token from the current MCP HTTP request."""
    try:
        from mcp.server.fastmcp import get_http_request  # FastMCP request context
        req = get_http_request()
        auth = req.headers.get("authorization", "")
    except Exception:
        return None
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def _resolve_scope(token: str | None) -> tuple[dict | None, list[int]]:
    """Resolve a token to (user, accessible_space_ids). ([] when unauthorized)."""
    if not token:
        return None, []
    with get_conn() as conn:
        user = user_by_token(conn, token)
        if not user:
            return None, []
        return user, accessible_space_ids(conn, user["id"])
```

> If `get_http_request` is unavailable in the installed FastMCP version, fall back to reading the token from a `token` tool argument. Verify the import path with: `ssh u24 'cd ~/projects/openbrain-rag && docker compose run --rm --entrypoint "" app python -c "from mcp.server.fastmcp import get_http_request; print(\"ok\")"'`. If that prints an ImportError, switch `_bearer_token` to accept the token via each tool's signature instead and adjust the tool tests accordingly.

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/mcp_server/test_tools.py -k resolve_scope -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp_server/server.py tests/mcp_server/test_tools.py
git commit -m "feat(mcp): bearer-token scope resolution helper"
```

### Task 6: Scope the three MCP tools

**Files:**
- Modify: `mcp_server/server.py`
- Test: `tests/mcp_server/test_tools.py`

- [ ] **Step 1: Write the failing tests**

The existing tool tests call `t_search("churn", top_k=5)` with no token and expect results. Tools now require a token. Add a fixture that stubs `_bearer_token` and `_resolve_scope` to a real user/scope, and rewrite the assertions. Add to `tests/mcp_server/test_tools.py`:

```python
@pytest.fixture
def as_user(db, monkeypatch):
    """Make MCP tools run as a freshly created user scoped to their spaces."""
    u = get_or_create_user(db, "tooluser")
    ids = accessible_space_ids(db, u["id"])
    import mcp_server.server as srv
    monkeypatch.setattr(srv, "_bearer_token", lambda: u["mcp_token"])
    # _resolve_scope opens its own connection; that's fine in-container.
    return {"user": u, "space_ids": ids}


def test_tool_search_requires_token(db, monkeypatch):
    import mcp_server.server as srv
    monkeypatch.setattr(srv, "_bearer_token", lambda: None)
    _mock_embed(monkeypatch, [0.1] * 768)
    out = srv.search("churn", top_k=5)
    assert out == {"error": "unauthorized"} or out == []  # see Step 3 for chosen shape
```

Replace `test_tool_search_returns_chunk_dicts`, `test_tool_search_respects_source_filter`, the namespace test, and `test_tool_list_documents_*` so they use the `as_user` fixture and seed into the user's accessible spaces. Example:

```python
def test_tool_search_scoped_to_user_spaces(db, monkeypatch, as_user):
    _mock_embed(monkeypatch, [0.1] * 768)
    common = ensure_common_space(db)
    other = get_or_create_user(db, "stranger")
    other_priv = max(set(accessible_space_ids(db, other["id"])) - {common})
    _seed_doc(db, "mine",   "churn", [0.1]*768, space_id=common)
    _seed_doc(db, "theirs", "churn", [0.1]*768, space_id=other_priv)
    import mcp_server.server as srv
    out = srv.search("churn", top_k=5)
    titles = [r["document_title"] for r in out]
    assert "mine" in titles and "theirs" not in titles


def test_tool_fetch_blocks_cross_space(db, as_user):
    common = ensure_common_space(db)
    other = get_or_create_user(db, "stranger2")
    other_priv = max(set(accessible_space_ids(db, other["id"])) - {common})
    doc_id = _seed_doc(db, "secret", "x", [0.1]*768, space_id=other_priv)
    import mcp_server.server as srv
    out = srv.fetch_document(doc_id)
    assert "error" in out


def test_tool_list_documents_scoped(db, as_user):
    common = ensure_common_space(db)
    other = get_or_create_user(db, "stranger3")
    other_priv = max(set(accessible_space_ids(db, other["id"])) - {common})
    _seed_doc(db, "visible", "x", [0.1]*768, space_id=common)
    _seed_doc(db, "hidden",  "x", [0.1]*768, space_id=other_priv)
    import mcp_server.server as srv
    rows = srv.list_documents()
    titles = {r["title"] for r in rows}
    assert "visible" in titles and "hidden" not in titles
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/mcp_server/test_tools.py -v`
Expected: FAIL — tools don't yet enforce scope; `search` has a `namespaces` param, not scoping.

- [ ] **Step 3: Implement scoping in the three tools**

`search` tool — replace `namespaces` with `spaces` (names) and enforce scope:

```python
@mcp.tool()
def search(query: str,
           source_types: list[str] | None = None,
           file_extensions: list[str] | None = None,
           spaces: list[str] | None = None,
           top_k: int = 12) -> list[dict]:
    """Hybrid pgvector ANN + tsvector BM25 retrieval, RRF-merged.

    Scoped to the spaces the caller's token can access. `spaces` (names)
    optionally narrows within accessible spaces. Requires a Bearer token.
    """
    user, scope_ids = _resolve_scope(_bearer_token())
    if user is None:
        return {"error": "unauthorized"}
    if spaces:
        from accounts.core import space_by_name
        with get_conn() as conn:
            wanted = [s["id"] for n in spaces
                      if (s := space_by_name(conn, n)) and s["id"] in scope_ids]
        scope_ids = wanted
    filters = SearchFilters(
        source_types=source_types or [],
        file_extensions=file_extensions or [],
        space_ids=scope_ids,
    )
    results = do_search(query, filters=filters, top_k=top_k)
    return [
        {"chunk_id": r.chunk_id, "document_id": r.document_id,
         "document_title": r.document_title, "source_type": r.source_type,
         "file_extension": r.file_extension, "content": r.content,
         "rrf_score": r.rrf_score}
        for r in results
    ]
```

Note: `search` may now return a dict on the unauthorized path; the test in Task 5 Step 1 asserts the `{"error": "unauthorized"}` shape — keep that shape.

`fetch_document` — add scope check:

```python
@mcp.tool()
def fetch_document(document_id: int) -> dict:
    user, scope_ids = _resolve_scope(_bearer_token())
    if user is None:
        return {"error": "unauthorized"}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.id, d.title, d.source_type, d.source_ref, d.ingested_at,
                       d.status, d.file_extension, d.file_size, d.space_id, d.raw_content,
                       (SELECT COUNT(*) FROM chunks c WHERE c.document_id = d.id) AS chunk_count
                  FROM documents d
                 WHERE d.id = %s AND d.active = TRUE AND d.space_id = ANY(%s)
                """,
                (document_id, scope_ids),
            )
            row = cur.fetchone()
            if not row:
                return {"error": f"document {document_id} not found or not accessible"}
            cols = [c.name for c in cur.description]
            doc = dict(zip(cols, row))
            doc["ingested_at"] = _serialize_ts(doc["ingested_at"])
            return doc
```

`list_documents` — replace the `namespace` param with `space` (name) and scope:

```python
@mcp.tool()
def list_documents(status: str | None = None, source: str | None = None,
                   space: str | None = None, limit: int = 50) -> list[dict]:
    user, scope_ids = _resolve_scope(_bearer_token())
    if user is None:
        return {"error": "unauthorized"}
    where = ["active = TRUE", "space_id = ANY(%s)"]
    args: list = [scope_ids]
    if status:
        where.append("status = %s"); args.append(status)
    if source:
        where.append("source_type = %s"); args.append(source)
    if space:
        from accounts.core import space_by_name
        with get_conn() as conn:
            sp = space_by_name(conn, space)
        where.append("space_id = %s"); args.append(sp["id"] if sp else -1)
    sql = f"""
        SELECT d.id, d.title, d.source_type, d.file_extension, d.status,
               d.file_size, d.space_id, d.ingested_at,
               (SELECT COUNT(*) FROM chunks c WHERE c.document_id = d.id) AS chunk_count
          FROM documents d
         WHERE {' AND '.join(where)}
         ORDER BY d.ingested_at DESC NULLS LAST
         LIMIT %s
    """
    args.append(limit)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            cols = [c.name for c in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            for r in rows:
                r["ingested_at"] = _serialize_ts(r["ingested_at"])
            return rows
```

Update the module docstring tool signatures accordingly (`spaces?` on search, `space?` on list_documents).

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/mcp_server/test_tools.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp_server/server.py tests/mcp_server/test_tools.py
git commit -m "feat(mcp): scope search/list/fetch to token user's spaces"
```

---

## Phase 4 — webui identity + spaces + scoped inventory/upload

### Task 7: Current-user dependency + `/login`

**Files:**
- Create: `webui/routes/auth.py`
- Create: `webui/templates/login.html`
- Modify: `webui/app.py` (register router)
- Test: `tests/webui/test_auth.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/webui/test_auth.py`:

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/webui/test_auth.py -v`
Expected: FAIL — no `/login`, inventory does not redirect.

- [ ] **Step 3: Implement `webui/routes/auth.py`**

```python
"""Lightweight username identity (no password) for the trusted LAN."""
from __future__ import annotations
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from db.connection import get_conn
from accounts.core import get_or_create_user
from webui.app import render

router = APIRouter()

COOKIE = "ob_user"


def current_user(request: Request) -> dict | None:
    """Resolve the logged-in user from the cookie, or None."""
    username = request.cookies.get(COOKIE)
    if not username:
        return None
    with get_conn() as conn:
        return get_or_create_user(conn, username)


def require_user(request: Request):
    """Dependency: return the user or a RedirectResponse to /login."""
    user = current_user(request)
    if user is None:
        return RedirectResponse(url="/login", status_code=307)
    return user


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return render(request, "login.html", {"page": "login"})


@router.post("/login")
async def login_submit(request: Request, username: str = Form(...)):
    with get_conn() as conn:
        user = get_or_create_user(conn, username)
    resp = RedirectResponse(url="/inventory", status_code=303)
    resp.set_cookie(COOKIE, user["username"], httponly=True, samesite="lax")
    return resp


@router.post("/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp
```

Create `webui/templates/login.html`:

```html
{% extends "base.html" %}
{% block title %}Sign in — OpenBrain{% endblock %}
{% block content %}
<div class="toolbar"><h2>Sign in</h2></div>
<form method="post" action="/login" class="filters">
  <input class="input" type="text" name="username" placeholder="username" autofocus required>
  <button class="btn primary" type="submit">Continue</button>
</form>
<p style="color: var(--muted); font-size: 12px;">
  No password — pick a username. New names are created automatically.
</p>
{% endblock %}
```

Register the router in `webui/app.py` (in `create_app`, alongside the other includes):

```python
    from webui.routes import upload, inventory, docgen, drafts, auth, spaces
    app.include_router(auth.router)
    app.include_router(upload.router)
    app.include_router(inventory.router)
    app.include_router(docgen.router)
    app.include_router(drafts.router)
    app.include_router(spaces.router)
```

(`spaces` is created in Task 9; if executing strictly in order, add `spaces` to this import in Task 9 and include only `auth` here.)

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/webui/test_auth.py -v`
Expected: the login tests PASS. `test_inventory_without_cookie_redirects_to_login` stays RED until Task 8 wires the dependency into the inventory route — note this and proceed; it goes green in Task 8.

- [ ] **Step 5: Commit**

```bash
git add webui/routes/auth.py webui/templates/login.html webui/app.py tests/webui/test_auth.py
git commit -m "feat(webui): username login + current-user dependency"
```

### Task 8: Scope inventory to the current user

**Files:**
- Modify: `webui/routes/inventory.py`
- Modify: `webui/templates/inventory.html`, `webui/templates/_inventory_rows.html`
- Test: `tests/webui/test_inventory.py`

- [ ] **Step 1: Write/adjust the failing tests**

Update `tests/webui/test_inventory.py`: the `client` must carry an `ob_user` cookie, and `_seed_doc` takes a `space_id`. Add a logged-in client fixture and rewrite tests:

```python
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


async def test_inventory_lists_accessible_docs(client, db):
    common = ensure_common_space(db)
    _seed_doc(db, title="visible", status="indexed", space_id=common)
    other = get_or_create_user(db, "other")
    other_priv = max(set(accessible_space_ids(db, other["id"])) - {common})
    _seed_doc(db, title="hidden", status="indexed", space_id=other_priv)
    db.commit()
    r = await client.get("/inventory")
    assert r.status_code == 200
    assert "visible" in r.text
    assert "hidden" not in r.text


async def test_inventory_space_filter(client, db):
    common = ensure_common_space(db)
    user = get_or_create_user(db, "invuser")
    priv = max(set(accessible_space_ids(db, user["id"])) - {common})
    _seed_doc(db, title="c-doc", status="indexed", space_id=common)
    _seed_doc(db, title="p-doc", status="indexed", space_id=priv)
    db.commit()
    r = await client.get(f"/inventory/rows?space={priv}")
    assert "p-doc" in r.text and "c-doc" not in r.text
```

Note: the `client` cookie user `invuser` is created lazily by `current_user`; `test_inventory_space_filter` references the same `invuser` so its personal space is the one seeded.

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/webui/test_inventory.py tests/webui/test_auth.py::test_inventory_without_cookie_redirects_to_login -v`
Expected: FAIL — inventory not scoped, no `space` filter, redirect not wired.

- [ ] **Step 3: Implement scoping in `webui/routes/inventory.py`**

Rewrite to depend on the user and scope by accessible spaces. Replace `namespace` plumbing with `space` (a space id) and add the spaces dropdown source:

```python
"""Inventory list + HTMX-polled partial (scoped to the current user)."""
from __future__ import annotations
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from db.connection import get_conn
from accounts.core import accessible_space_ids, list_spaces_for
from webui.app import render
from webui.routes.auth import current_user

router = APIRouter()


def _query_docs(*, space_ids: list[int], status, source, ftype, space, search,
                limit=200) -> list[dict]:
    where = ["active = TRUE", "space_id = ANY(%s)"]
    args: list = [space_ids]
    if status:  where.append("status = %s");          args.append(status)
    if source:  where.append("source_type = %s");     args.append(source)
    if ftype:   where.append("file_extension = %s");  args.append(ftype)
    if space:   where.append("space_id = %s");        args.append(int(space))
    if search:  where.append("title ILIKE %s");       args.append(f"%{search}%")
    sql = f"""
        SELECT d.id, d.title, d.source_type, d.file_extension, d.status,
               s.name AS space_name, d.failure_reason, d.ingested_at,
               (SELECT COUNT(*) FROM chunks c WHERE c.document_id = d.id) AS chunk_count
          FROM documents d JOIN spaces s ON s.id = d.space_id
         WHERE {' AND '.join(where)}
         ORDER BY d.ingested_at DESC NULLS LAST
         LIMIT {limit}
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            cols = [c.name for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def _counts(space_ids: list[int]) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE active AND status='indexed') AS indexed,
                    COUNT(*) FILTER (WHERE active AND status IN
                        ('queued','processing','transcribing'))          AS processing,
                    COUNT(*) FILTER (WHERE active AND status='failed')   AS failed
                FROM documents WHERE space_id = ANY(%s)
            """, (space_ids,))
            row = cur.fetchone()
            return {"indexed": row[0], "processing": row[1], "failed": row[2]}


@router.get("/inventory", response_class=HTMLResponse)
async def inventory(request: Request, status=None, source=None, ftype=None,
                    space=None, search=None):
    user = current_user(request)
    if user is None:
        return RedirectResponse(url="/login", status_code=307)
    with get_conn() as conn:
        space_ids = accessible_space_ids(conn, user["id"])
        spaces = list_spaces_for(conn, user["id"])
    return render(request, "inventory.html", {
        "page": "inventory",
        "rows": _query_docs(space_ids=space_ids, status=status, source=source,
                            ftype=ftype, space=space, search=search),
        "counts": _counts(space_ids),
        "spaces": spaces,
        "user": user,
        "filters": {"status": status or "", "source": source or "",
                    "ftype": ftype or "", "space": space or "", "search": search or ""},
    })


@router.get("/inventory/rows", response_class=HTMLResponse)
async def inventory_rows(request: Request, status=None, source=None, ftype=None,
                         space=None, search=None):
    user = current_user(request)
    if user is None:
        return RedirectResponse(url="/login", status_code=307)
    with get_conn() as conn:
        space_ids = accessible_space_ids(conn, user["id"])
    return render(request, "_inventory_rows.html", {
        "rows": _query_docs(space_ids=space_ids, status=status, source=source,
                            ftype=ftype, space=space, search=search),
    })
```

- [ ] **Step 4: Update templates**

`webui/templates/inventory.html` — replace the namespace `<select>` with a space `<select>` keyed by id, and the column header `Namespace`→`Space`:

```html
  <select class="input" name="space">
    <option value="">All spaces</option>
    {% for s in spaces %}
      <option value="{{ s.id }}" {% if filters.space == s.id|string %}selected{% endif %}>{{ s.name }}</option>
    {% endfor %}
  </select>
```

Header row: change the `<th>Namespace</th>` cell to `<th>Space</th>`.

`webui/templates/_inventory_rows.html` — change the namespace cell to `space_name`:

```html
  <td style="color: var(--muted)">{{ row.space_name }}</td>
```

(colspan stays 7.)

- [ ] **Step 5: Run to verify it passes**

Run: `pytest tests/webui/test_inventory.py tests/webui/test_auth.py -v`
Expected: PASS (including the redirect test now).

- [ ] **Step 6: Commit**

```bash
git add webui/routes/inventory.py webui/templates/inventory.html webui/templates/_inventory_rows.html tests/webui/test_inventory.py
git commit -m "feat(webui): scope inventory to current user's spaces"
```

### Task 9: `/spaces` page (list/create/join)

**Files:**
- Create: `webui/routes/spaces.py`
- Create: `webui/templates/spaces.html`
- Modify: `webui/app.py` (ensure `spaces.router` registered — see Task 7 Step 3)
- Test: `tests/webui/test_spaces.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/webui/test_spaces.py`:

```python
import pytest
from httpx import AsyncClient, ASGITransport
from webui.app import create_app
from accounts.core import get_or_create_user, create_shared_space, accessible_space_ids


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
    from accounts.core import space_by_name
    sp = space_by_name(db, "Research")
    assert sp["id"] in accessible_space_ids(db, user["id"])
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/webui/test_spaces.py -v`
Expected: FAIL — no `/spaces` routes.

- [ ] **Step 3: Implement `webui/routes/spaces.py`**

```python
"""Manage spaces: list accessible, create shared, join shared."""
from __future__ import annotations
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from db.connection import get_conn
from accounts.core import list_spaces_for, create_shared_space, join_space_by_name
from webui.app import render
from webui.routes.auth import current_user

router = APIRouter()


def _user_or_redirect(request: Request):
    user = current_user(request)
    if user is None:
        return None, RedirectResponse(url="/login", status_code=307)
    return user, None


@router.get("/spaces", response_class=HTMLResponse)
async def spaces_page(request: Request, error: str | None = None):
    user, redirect = _user_or_redirect(request)
    if redirect:
        return redirect
    with get_conn() as conn:
        spaces = list_spaces_for(conn, user["id"])
    return render(request, "spaces.html",
                  {"page": "spaces", "spaces": spaces, "user": user, "error": error})


@router.post("/spaces/create")
async def spaces_create(request: Request, name: str = Form(...)):
    user, redirect = _user_or_redirect(request)
    if redirect:
        return redirect
    try:
        with get_conn() as conn:
            create_shared_space(conn, name, user["id"])
    except ValueError as e:
        return RedirectResponse(url=f"/spaces?error={e}", status_code=303)
    return RedirectResponse(url="/spaces", status_code=303)


@router.post("/spaces/join")
async def spaces_join(request: Request, name: str = Form(...)):
    user, redirect = _user_or_redirect(request)
    if redirect:
        return redirect
    try:
        with get_conn() as conn:
            join_space_by_name(conn, name, user["id"])
    except ValueError as e:
        return RedirectResponse(url=f"/spaces?error={e}", status_code=303)
    return RedirectResponse(url="/spaces", status_code=303)
```

Create `webui/templates/spaces.html`:

```html
{% extends "base.html" %}
{% block title %}Spaces — OpenBrain{% endblock %}
{% block content %}
<div class="toolbar"><h2>Spaces</h2></div>
{% if error %}<p style="color: var(--danger, #c00)">{{ error }}</p>{% endif %}
<table>
  <thead><tr><th>Name</th><th>Kind</th><th>Role</th><th style="text-align:right">Docs</th></tr></thead>
  <tbody>
    {% for s in spaces %}
    <tr><td>{{ s.name }}</td><td style="color: var(--muted)">{{ s.kind }}</td>
        <td style="color: var(--muted)">{{ s.role }}</td>
        <td style="text-align:right">{{ s.doc_count }}</td></tr>
    {% endfor %}
  </tbody>
</table>
<div class="toolbar" style="margin-top:24px"><h3>Create shared space</h3></div>
<form method="post" action="/spaces/create" class="filters">
  <input class="input" type="text" name="name" placeholder="new space name" required>
  <button class="btn primary" type="submit">Create</button>
</form>
<div class="toolbar" style="margin-top:16px"><h3>Join shared space</h3></div>
<form method="post" action="/spaces/join" class="filters">
  <input class="input" type="text" name="name" placeholder="existing space name" required>
  <button class="btn" type="submit">Join</button>
</form>
{% endblock %}
```

Ensure `spaces.router` is included in `webui/app.py` (Task 7 Step 3 import line). Add a sidebar link to `/spaces` in `webui/templates/base.html` next to the existing nav links (match the existing link markup).

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/webui/test_spaces.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add webui/routes/spaces.py webui/templates/spaces.html webui/app.py webui/templates/base.html tests/webui/test_spaces.py
git commit -m "feat(webui): /spaces page to create and join shared spaces"
```

### Task 10: Upload into a chosen space

**Files:**
- Modify: `webui/routes/upload.py`
- Modify: `webui/templates/upload.html`
- Test: `tests/webui/test_upload.py`

- [ ] **Step 1: Inspect current upload namespace handling**

Run: `sed -n '1,200p' webui/routes/upload.py` and note where `namespace` is read from the form and passed to `insert_document`. The change: read `space` (a space id) from the form, validate it is in the user's accessible spaces, and pass `space_id` + `created_by` to `insert_document`.

- [ ] **Step 2: Write the failing test**

In `tests/webui/test_upload.py`, give the client an `ob_user` cookie and assert the uploaded doc lands in the chosen space with `created_by` set. Mirror the existing upload test, adding:

```python
async def test_upload_assigns_space_and_creator(client, db, tmp_upload_dir):
    from accounts.core import get_or_create_user, accessible_space_ids, ensure_common_space
    user = get_or_create_user(db, "upl"); db.commit()
    common = ensure_common_space(db)
    files = {"files": ("n.md", b"# hi", "text/markdown")}
    r = await client.post("/upload", data={"space": str(common)}, files=files)
    assert r.status_code in (200, 303)
    with db.cursor() as cur:
        cur.execute("SELECT space_id, created_by FROM documents WHERE title='n' OR source_ref LIKE '%n.md'")
        row = cur.fetchone()
    assert row is not None and row["space_id"] == common
```

(Adapt to the existing upload test's client fixture — add `c.cookies.set("ob_user", "upl")`.)

- [ ] **Step 3: Run to verify it fails**

Run: `pytest tests/webui/test_upload.py -v`
Expected: FAIL — upload still uses `namespace`.

- [ ] **Step 4: Implement**

In `webui/routes/upload.py`: resolve `current_user`; read `space` form field (default the user's Common id); validate `int(space) in accessible_space_ids`; pass `space_id=...`, `created_by=user["id"]` wherever `insert_document` / the queue enqueues. Remove `namespace`/`SUGGESTED_NAMESPACES` usage. Populate the upload form's space `<select>` from `list_spaces_for`.

`webui/templates/upload.html`: replace the namespace datalist input with a space `<select>`:

```html
  <select class="input" name="space">
    {% for s in spaces %}
      <option value="{{ s.id }}">{{ s.name }}</option>
    {% endfor %}
  </select>
```

Pass `spaces=list_spaces_for(conn, user["id"])` from the GET handler that renders `upload.html`.

- [ ] **Step 5: Remove dead namespace constants**

Now safe: delete `DEFAULT_NAMESPACE` and `SUGGESTED_NAMESPACES` from `ingestion/core.py` if no remaining importers (`grep -rn "SUGGESTED_NAMESPACES\|DEFAULT_NAMESPACE" --include=*.py .`). `ingest_local.py` still imports `DEFAULT_NAMESPACE` until Task 11 — do this deletion in Task 11 if the grep shows it is still referenced.

- [ ] **Step 6: Run to verify it passes**

Run: `pytest tests/webui/test_upload.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add webui/routes/upload.py webui/templates/upload.html tests/webui/test_upload.py
git commit -m "feat(webui): upload into a chosen space with creator"
```

---

## Phase 5 — CLI + watcher

### Task 11: CLI `--space` / `--user`

**Files:**
- Modify: `ingestion/ingest_local.py`
- Modify: `ingestion/core.py` (remove namespace constants)
- Test: `tests/ingestion/test_ingest_local.py` (create if absent)

- [ ] **Step 1: Write the failing test**

Create/extend `tests/ingestion/test_ingest_local.py`:

```python
from pathlib import Path
from accounts.core import ensure_common_space, get_or_create_user, space_by_name


def test_ingest_file_routes_to_space(db, monkeypatch, tmp_path):
    import ingestion.ingest_local as il
    import ingestion.core as core
    monkeypatch.setattr(core, "embed", lambda *a, **k: [0.1] * 768)
    common = ensure_common_space(db)
    p = tmp_path / "note.md"; p.write_text("hello world")
    il.ingest_file(db, p, dry_run=False, space_id=common, created_by=None)
    db.commit()
    with db.cursor() as cur:
        cur.execute("SELECT space_id FROM documents WHERE title='note'")
        assert cur.fetchone()["space_id"] == common
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/ingestion/test_ingest_local.py -v`
Expected: FAIL — `ingest_file` takes `namespace`, not `space_id`.

- [ ] **Step 3: Implement**

In `ingestion/ingest_local.py`:
- Remove the `from ingestion.core import (DEFAULT_NAMESPACE, ...)` namespace import.
- `insert_document` wrapper and `ingest_file`/`ingest_folder` take `space_id: int` and `created_by: int | None = None` instead of `namespace`. Pass them through to `ingestion.core.insert_document(... space_id=space_id, created_by=created_by ...)`. Drop the `metadata={"namespace": ...}` bits.
- `main()`: replace `--namespace` with `--space <name>` (default `Common`) and add `--user <username>`. Resolve via `accounts.core`:

```python
    parser.add_argument("--space", default="Common", help="Target space name (default: Common)")
    parser.add_argument("--user", default=None, help="Username to attribute ingestion to")
    args = parser.parse_args()

    from accounts.core import (ensure_common_space, space_by_name,
                               get_or_create_user)
    with get_conn() as conn:
        created_by = get_or_create_user(conn, args.user)["id"] if args.user else None
        if args.space.strip().lower() == "common":
            space_id = ensure_common_space(conn)
        else:
            sp = space_by_name(conn, args.space)
            if not sp:
                parser.error(f"shared space '{args.space}' not found")
            space_id = sp["id"]

    if args.file:
        with get_conn() as conn:
            ingest_file(conn, args.file, dry_run=args.dry_run,
                        space_id=space_id, created_by=created_by)
    else:
        ingest_folder(args.folder, dry_run=args.dry_run,
                      space_id=space_id, created_by=created_by)
```

Then remove `DEFAULT_NAMESPACE`/`SUGGESTED_NAMESPACES` from `ingestion/core.py` (grep first to confirm no importers remain).

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/ingestion/test_ingest_local.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ingestion/ingest_local.py ingestion/core.py tests/ingestion/test_ingest_local.py
git commit -m "feat(cli): ingest_local --space/--user; drop namespace constants"
```

### Task 12: Watcher ingests into `VB_WATCH_SPACE`

**Files:**
- Modify: `config.py` (add `WATCH_SPACE`)
- Modify: `webui/workers/folder_watcher.py`
- Test: `tests/workers/test_file_dispatch.py` (or the watcher test that exists)

- [ ] **Step 1: Inspect current watcher namespace derivation**

Run: `sed -n '1,200p' webui/workers/folder_watcher.py` — find where it derives `namespace` from the first subfolder and passes it onward. Replace with a fixed target space resolved from config.

- [ ] **Step 2: Write the failing test**

In the watcher's test module, assert a watched file lands in the configured space (Common by default). Mirror the existing dispatch test, asserting the enqueued/inserted doc's `space_id` equals Common. (Use the existing test's harness; add an assertion on `space_id` and remove any `namespace` assertions.)

- [ ] **Step 3: Implement**

`config.py` — add near the watch settings:

```python
WATCH_SPACE = os.getenv("VB_WATCH_SPACE", "Common")
```

`webui/workers/folder_watcher.py` — remove the subfolder→namespace logic; resolve the target space id once (via `accounts.core.ensure_common_space` when `WATCH_SPACE=='Common'`, else `space_by_name`), and pass `space_id` + `created_by=None` along the ingest path it uses. Match the existing call signature into `insert_document` / the queue.

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/workers/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add config.py webui/workers/folder_watcher.py tests/workers/
git commit -m "feat(watcher): ingest into VB_WATCH_SPACE (default Common)"
```

---

## Phase 6 — Full verification + docs

### Task 13: Full suite green on fresh schema

**Files:** none (verification) — fix any stragglers found.

- [ ] **Step 1: Drop + reload test schema, run the whole suite**

```bash
ssh u24 'cd ~/projects/openbrain-rag && docker exec openbrain-rag-db-1 psql -U postgres -d openbrain_test -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" && docker compose run --rm -e VB_DB_HOST=db -e VB_DB_PORT=5432 -e VB_DB_NAME=openbrain_test --entrypoint "" app python -m pytest -q 2>&1 | tail -20'
```

Expected: all tests PASS. If the e2e smoke test (`tests/test_smoke_e2e.py`) seeds documents or calls upload/docgen, update it to provide a `space_id` / `ob_user` cookie as needed.

- [ ] **Step 2: Migration check on real data**

```bash
ssh u24 'cd ~/projects/openbrain-rag && docker compose up -d db && docker compose exec db psql -U postgres -d openbrain -c "SELECT count(*) FROM documents WHERE space_id IS NULL"'
```

Expected: `0` (all real docs migrated into Common). Confirm `namespace` column is gone:
`docker compose exec db psql -U postgres -d openbrain -c "\d documents"`.

- [ ] **Step 3: Live smoke — app + login + scoped inventory**

```bash
ssh u24 'cd ~/projects/openbrain-rag && docker compose up -d --build app && sleep 6 && curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/inventory'
```

Expected: `307` (redirect to `/login`). Then verify `/login` is `200`.

- [ ] **Step 4: Commit any fixes**

```bash
git add -A && git commit -m "test: align e2e + smoke with multi-user spaces"
```

### Task 14: Documentation

**Files:**
- Modify: `README.md`
- Modify: `.env.example` (add `VB_WATCH_SPACE`)

- [ ] **Step 1: Update README**

Add a "Multi-user spaces" section: lightweight username login (no password), the Common auto-join space, personal spaces, creating/joining shared spaces at `/spaces`, MCP `Authorization: Bearer <mcp_token>` (where to find a user's token), CLI `--space`/`--user`, and `VB_WATCH_SPACE`. Update the folder-watcher section (it no longer derives namespace from subfolders). Note that isolation is honor-system on a trusted LAN, not a security boundary.

- [ ] **Step 2: Update `.env.example`**

Add: `VB_WATCH_SPACE=Common`.

- [ ] **Step 3: Commit + push**

```bash
git add README.md .env.example
git commit -m "docs: multi-user spaces (login, spaces, MCP token, VB_WATCH_SPACE)"
git push origin feat/webui
```

---

## Self-Review

**Spec coverage:**
- Users/spaces/space_members + Common seed → Task 1. ✅
- Access helper (get_or_create_user, accessible_space_ids, create/join, list) → Task 2. ✅
- documents space_id/created_by + namespace→Common migration + legacy preservation → Task 3. ✅
- search space_ids fail-closed → Task 4. ✅
- MCP bearer-token auth + scoping for search/list/fetch → Tasks 5–6. ✅
- webui login + current-user dependency → Task 7. ✅
- inventory scoping + space dropdown/column → Task 8. ✅
- `/spaces` create/join → Task 9. ✅
- upload into chosen space + created_by → Task 10. ✅
- CLI `--space`/`--user` → Task 11. ✅
- watcher `VB_WATCH_SPACE` → Task 12. ✅
- error handling (redirect, fail-closed, unauthorized, duplicate, cross-space) → Tasks 4,6,7,9. ✅
- testing matrix → covered per task. ✅
- README/docs → Task 14. ✅

**Type/name consistency:** helper names (`ensure_common_space`, `get_or_create_user`, `user_by_token`, `accessible_space_ids`, `create_shared_space`, `space_by_name`, `join_space_by_name`, `list_spaces_for`) are used identically across Tasks 2–12. `SearchFilters.space_ids`, `insert_document(space_id=, created_by=)`, MCP `spaces`/`space` params, webui `space` query/form field — consistent.

**Known cross-phase red window:** after Task 3, `ingest_local.py`/`upload.py`/`folder_watcher.py` still reference `namespace` and are red until their Phase-4/5 tasks land. Full-suite green is asserted only at Task 13. This is intentional and called out at each task.

**Risk:** FastMCP `get_http_request` import path (Task 5) — verification command + fallback documented inline.
