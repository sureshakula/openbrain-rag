# Multi-User Spaces — Design

**Date:** 2026-06-17
**Status:** Approved (pending spec review)
**Branch:** feat/webui

## Summary

Add multi-user isolation to openbrain-rag with an option to share knowledge to
common/shared spaces. Today the system has no concept of users, ownership, or
access scope: every document is visible to everyone hitting the webui or MCP
server. This design introduces **users**, **spaces**, and **membership**, and
unifies the existing free-form `namespace` column into the space concept (a
document lives in exactly one space).

This is an honor-system isolation model for a trusted single-machine LAN
deployment (Mac Mini M4 / internal VOZIQ use), not a hardened security boundary.
Enforcement is app-layer; the schema is shaped so Postgres Row-Level Security
can be layered on later without a migration if hardening is ever needed.

## Goals

- Each user sees only documents they can access: their own private space, the
  built-in Common space, and any shared spaces they have joined.
- Users can create named shared spaces and join existing ones.
- A built-in **Common** shared space that every user auto-joins is the default
  landing zone for server-side ingestion (folder watcher, CLI) and for all
  pre-existing documents.
- All four ingestion/retrieval surfaces (webui, MCP, CLI, folder watcher) become
  space-aware.

## Non-Goals

- Passwords, password hashing, real sessions, or SSO. Authentication is a
  lightweight self-asserted username on a trusted LAN.
- Hard security / cryptographic isolation. Isolation is organizational and
  honor-system. A malicious LAN user with a valid token is out of scope.
- Per-document sharing to individual users, or per-document ACLs. Sharing is at
  the space level only.
- Postgres RLS in this iteration (schema is shaped to allow it later).

## Decisions (from brainstorming)

1. **Auth:** lightweight self-asserted username (no password), stored in a
   cookie for the webui; a per-user opaque token for MCP/CLI. First-seen
   username auto-registers.
2. **Sharing model:** multiple named shared spaces (team-like), plus one
   personal space per user.
3. **Namespace unification:** the existing `namespace` column is **replaced** by
   spaces. A document belongs to exactly one space. Old namespace values are
   preserved into `metadata.namespace_legacy` for reference.
4. **Common space:** a built-in shared space all users auto-join; default for
   server-side ingestion and existing-doc migration.
5. **Enforcement:** app-layer filtering via a single access helper. No RLS yet.

## Data Model

New tables (added to `db/schema.sql`, idempotent):

```sql
users (
    id          SERIAL PRIMARY KEY,
    username    TEXT NOT NULL UNIQUE,
    mcp_token   TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)

spaces (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('personal','shared')),
    created_by  INTEGER REFERENCES users(id),   -- NULL for the seeded Common space
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)

space_members (
    space_id    INTEGER NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL REFERENCES users(id)  ON DELETE CASCADE,
    role        TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('owner','member')),
    PRIMARY KEY (space_id, user_id)
)
```

`documents` changes:

```sql
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS space_id   INTEGER REFERENCES spaces(id),
    ADD COLUMN IF NOT EXISTS created_by INTEGER REFERENCES users(id);
-- after backfill: space_id set NOT NULL; namespace column dropped
CREATE INDEX IF NOT EXISTS idx_documents_space ON documents(space_id);
```

Naming/uniqueness rules:
- **Common** space: `name='Common'`, `kind='shared'`, `created_by=NULL`. Seeded
  once. Stable lookup by `(name='Common', kind='shared')`.
- **Personal** space: created per user, `kind='personal'`, `name` = username,
  `created_by` = that user. Creator is `role='owner'` member.
- Shared space names are unique among `kind='shared'` (case-insensitive,
  trimmed/lowercased on input). Personal space names are not globally unique
  (scoped by owner).

## Access Helper

New module `accounts/` (e.g. `accounts/core.py`) — the single source of truth
all surfaces call. Functions:

- `get_or_create_user(conn, username) -> dict` — trims/lowercases username;
  inserts user with a generated `mcp_token` (e.g. `secrets.token_urlsafe(24)`);
  creates the personal space and owner membership; joins Common. Idempotent on
  username. Returns `{id, username, mcp_token}`.
- `user_by_token(conn, token) -> dict | None` — resolve MCP token to user.
- `accessible_space_ids(conn, user_id) -> list[int]` — all space ids the user is
  a member of (personal + Common + joined shared).
- `create_shared_space(conn, name, owner_user_id) -> dict` — create a shared
  space, add owner membership. Rejects duplicate shared-space name.
- `join_space(conn, space_id, user_id)` / lookup by name.
- `list_spaces_for(conn, user_id) -> list[dict]` — accessible spaces with id,
  name, kind, role, doc_count, for dropdowns and the spaces page.

Bootstrap: ensure the Common space row exists at startup (seeded in schema.sql,
plus a defensive get-or-create in the helper).

## Surface Changes

### Retrieval — `retrieval/search.py`
- `SearchFilters.namespaces: list[str]` → `SearchFilters.space_ids: list[int]`.
- `_build_where` emits `d.space_id = ANY(%s)`.
- Search is **always** scoped. Callers (webui, MCP) resolve accessible space ids
  and pass them. Passing an empty `space_id` list returns nothing (fail-closed),
  not everything.

### webui
- **Identity:** cookie `ob_user` holds the username. A FastAPI dependency
  `current_user` resolves it via `get_or_create_user`; if the cookie is absent,
  redirect to `GET /login`.
- **`/login`:** minimal page, single username field, no password. `POST /login`
  calls `get_or_create_user`, sets the `ob_user` cookie, redirects to inventory.
- **Inventory:** `_query_docs` scoped to `accessible_space_ids`; the namespace
  filter dropdown becomes an accessible-**spaces** dropdown (`space` query
  param, filter by `space_id`); a **Space** column replaces the Namespace column.
- **Upload:** a target-space `<select>` populated from `list_spaces_for`
  (default Common); the upload path sets `space_id` and `created_by`.
- **`/spaces`:** new page — list accessible spaces (name, kind, role, doc_count);
  create a shared space; join an existing shared space by name.
- **docgen / drafts:** retrieval scoped to the current user's accessible spaces.

### MCP — `mcp_server/server.py`
- **Auth:** read `Authorization: Bearer <token>` from the FastMCP request
  context; resolve via `user_by_token`. Missing/invalid token → tool returns an
  auth error (no data). 
- **Scoping:** `search`, `list_documents`, `fetch_document` all scope to the
  token user's accessible spaces. `fetch_document` returns an error if the
  document's space is not accessible.
- `search` / `list_documents`: the `namespaces` / `namespace` params become
  `spaces` / `space` (by name), validated against accessible spaces. Output rows
  include `space` (name) in place of `namespace`.

### CLI — `ingestion/ingest_local.py`
- `--namespace` → `--space <name>` (default `Common`).
- `--user <username>` (optional) → resolves/creates the user; `created_by` set
  to that user, or `NULL` when omitted (system ingestion).
- Resolve the space by name; error clearly if a shared space name does not exist
  (CLI does not auto-create shared spaces).

### Folder watcher — `webui/workers/folder_watcher.py`
- Drop the subfolder→namespace derivation.
- Ingest into the space named by `VB_WATCH_SPACE` (default `Common`),
  `created_by=NULL`.

## Migration

Implemented inside the idempotent `db/schema.sql` so a plain startup/test load
performs it, and re-running is safe:

1. `CREATE TABLE IF NOT EXISTS` for users, spaces, space_members.
2. Seed the Common space if absent
   (`INSERT ... SELECT WHERE NOT EXISTS`).
3. `ALTER TABLE documents ADD COLUMN IF NOT EXISTS space_id`, `created_by`.
4. Backfill: for rows with `space_id IS NULL`, set `space_id` = Common's id and
   copy the old `namespace` into `metadata->'namespace_legacy'` (only if a
   `namespace` column still exists — guarded so the step is a no-op after the
   column is dropped).
5. `ALTER TABLE documents ALTER COLUMN space_id SET NOT NULL`.
6. `ALTER TABLE documents DROP COLUMN IF EXISTS namespace` and drop its index.

Ordering matters: steps 4–6 must run after 1–3. Because `schema.sql` is applied
top-to-bottom on every startup and in tests, the block is written to be
idempotent (guards via `IF EXISTS` / `WHERE NOT EXISTS` / `IS NULL`).

## Error Handling

- **No cookie (webui):** redirect to `/login`; never 500.
- **Empty accessible spaces:** fail-closed (return no rows). A freshly created
  user always has at least personal + Common, so this is effectively
  unreachable, but the query must not fall back to "all".
- **MCP missing/invalid token:** tools return `{"error": "unauthorized"}`; no
  data leaks.
- **Duplicate shared-space name:** `create_shared_space` raises a clear error;
  webui surfaces it inline.
- **CLI unknown space:** exit non-zero with a clear message listing that the
  space must exist (Common always exists).
- **fetch_document across space boundary:** returns
  `{"error": "not found or not accessible"}` — indistinguishable from a missing
  doc, to avoid confirming existence of inaccessible docs.

## Testing

- **Access helper:** get-or-create idempotency; personal space + Common
  membership created; `accessible_space_ids` returns the right set; create/join
  shared space; duplicate-name rejection; token resolution.
- **Search scoping:** user sees own-private + Common; does NOT see another
  user's private space; space filter narrows correctly; empty accessible set
  returns nothing.
- **MCP:** valid token scopes results; missing/invalid token returns error;
  `fetch_document` blocks cross-space access; `space` param validated.
- **webui:** no cookie → redirect to `/login`; login sets cookie; inventory
  scoped; space dropdown lists accessible spaces; upload writes `space_id` +
  `created_by`; `/spaces` create + join.
- **CLI:** `--space` routes to the right space; unknown space errors; `--user`
  sets `created_by`.
- **Watcher:** ingests into `VB_WATCH_SPACE` / Common.
- **Migration:** a pre-existing doc with a legacy `namespace` lands in Common
  with `namespace_legacy` preserved; re-running schema.sql is a no-op.

## Implementation Order (for the plan)

1. Schema + migration + access helper (`accounts/core.py`) with tests.
2. `retrieval/search.py` space scoping.
3. MCP token auth + scoping.
4. webui: identity dependency + `/login`, inventory scoping, upload, `/spaces`.
5. CLI + watcher.
6. End-to-end + migration tests; docs/README update.

## Open Risks

- FastMCP request-context header access: confirm the supported API for reading
  the `Authorization` header inside a tool function; if unavailable, fall back to
  a token passed as a tool argument (less clean) — to be verified in phase 3.
- `schema.sql` now carries a data migration; it must stay idempotent. Tests load
  it repeatedly (conftest), which is the guard.
