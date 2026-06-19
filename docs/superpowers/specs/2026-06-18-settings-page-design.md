# Settings Page — Design

**Date:** 2026-06-18
**Status:** Approved (pending spec review)
**Branch:** feat/webui

## Summary

A `/settings` page to change watch + ingestion configuration at runtime, stored in a `settings` key/value table that overrides the env/`config.py` defaults. Values are read at their natural runtime points: the upload size limit and the watcher's target space apply immediately; watcher directory/debounce/enable and ingest worker count are wired at app startup, so they apply after an app restart (the page labels each accordingly). The watch folder is chosen as a **subfolder under the mounted watch dir** (`/data/watch`), respecting the in-container mount constraint.

## Decisions (from brainstorming)

1. **Scope:** watch + ingestion settings (six keys below).
2. **Watch folder:** a subfolder under the existing mounted watch dir — no compose/container change.
3. **Apply semantics:** live where cheap (upload limit, watch target space); on app restart for startup-wired values (watcher dir/debounce/enable, worker count). No hot-restart of the watcher in this iteration.

## Goals

- A logged-in user edits watch + ingestion settings in the webui; values persist in the DB and override env defaults.
- Settings with no DB row fall back to the env/`config.py` value (nothing breaks if unset).
- The page clearly marks which settings apply immediately vs after a restart.

## Non-Goals

- Hot-restarting the watcher or worker pool on save (restart-to-apply is acceptable).
- Per-user settings (settings are global/single-machine, honor-system).
- Editing arbitrary host paths or remounting volumes from the UI.
- Synthesis/model settings (out of scope this iteration).

## Settings Keys

| Key | Type | Env fallback (`config.py`) | Applies |
|---|---|---|---|
| `watch_enabled` | bool | watcher on iff `WATCH_DIR` non-empty | on restart |
| `watch_subfolder` | str | "" (watch the mount root) | on restart |
| `watch_debounce_sec` | int | `WATCH_DEBOUNCE_SEC` (2) | on restart |
| `watch_space` | str (space name) | `WATCH_SPACE` ("Common") | live |
| `ingest_worker_count` | int | `INGEST_WORKER_COUNT` (2) | on restart |
| `max_upload_mb` | int | `MAX_UPLOAD_MB` (500) | live |

## Data Model

Append to `db/schema.sql` (after the CHAT block):
```sql
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```
`tests/conftest.py` per-test TRUNCATE gains `settings` (no FK; order-independent, add it near the front).

## `settings/core.py`

A typed accessor with an env-fallback defaults map. It does NOT import the live `config` constants directly into a frozen map at import time — it reads `config.<NAME>` at call time so test reloads/env changes are honored.

```python
DEFAULTS = {
    "watch_enabled":        lambda: bool(config.WATCH_DIR),
    "watch_subfolder":      lambda: "",
    "watch_debounce_sec":   lambda: config.WATCH_DEBOUNCE_SEC,
    "watch_space":          lambda: config.WATCH_SPACE,
    "ingest_worker_count":  lambda: config.INGEST_WORKER_COUNT,
    "max_upload_mb":        lambda: config.MAX_UPLOAD_MB,
}

EDITABLE_KEYS = list(DEFAULTS)

def get(conn, key) -> str            # DB value or str(default)
def get_int(conn, key) -> int
def get_bool(conn, key) -> bool
def set_setting(conn, key, value)    # UPSERT; only EDITABLE_KEYS allowed
def all_settings(conn) -> list[dict] # [{key, value, default, is_overridden, applies}]
```

- `get` returns the DB row's value when present, else `str(DEFAULTS[key]())`.
- `get_bool` treats `"1"/"true"/"yes"/"on"` (case-insensitive) as True.
- `set_setting` raises `ValueError` for an unknown key.
- An `APPLIES = {key: "live"|"restart"}` map drives the UI labels: `watch_space` and `max_upload_mb` are `live`, the rest `restart`.

## Validation

Performed in the route before `set_setting`:
- `watch_debounce_sec`, `ingest_worker_count`, `max_upload_mb`: must parse as `int >= 1` (worker count `>= 1`, debounce `>= 0` allowed — use `>= 0` for debounce, `>= 1` for the others).
- `watch_subfolder`: reject if it contains `..`, a leading `/`, or a backslash (must be a relative path under the mount). Empty is allowed (root).
- `watch_enabled`: checkbox → "true"/"false".
- `watch_space`: free text; not validated here — resolved at ingest time, falling back to Common if it does not match a space.
- On any invalid field, re-render the form with an inline error and DO NOT persist.

## Wire-ins

- **`webui/routes/upload.py`** — replace `config.MAX_UPLOAD_MB` (line ~40/44) with `settings.get_int(conn, "max_upload_mb")` read inside a `get_conn()` block (the route already opens a connection; reuse it). Both the limit check and the 413 message use this value.
- **`webui/workers/folder_watcher.py`** — the per-ingest target space currently comes from `config.WATCH_SPACE`. Change `_resolve_space_id` (or its caller) to read `settings.get(conn, "watch_space")` so a saved value applies live. (The watcher already opens a `get_conn()` connection per ingest.)
- **`webui/app.py` lifespan** — at startup, open a short-lived `get_conn()` and read: `watch_enabled` (bool), `watch_subfolder` (str), `watch_debounce_sec` (int), `ingest_worker_count` (int) via `settings`. Compute the effective watch path = `Path(config.WATCH_DIR) / watch_subfolder` (only when `watch_enabled` AND `config.WATCH_DIR` set AND the path is a directory). Start the queue with the settings worker count. Keeps the existing "WATCH_DIR not a dir → warn + disable" behavior.

## webui

- **`GET /settings`** — auth-gated (redirect `/login` if no cookie). Render `settings.html` with `all_settings(conn)` (each row shows current value, default, overridden?, and the live/restart label). A banner notes restart-applied keys take effect on the next app restart.
- **`POST /settings`** — auth-gated. Validate all fields; on success `set_setting` each editable key and redirect back to `/settings` (303) with a success flash; on validation error re-render with the message.
- Nav link "Settings" in `base.html`.

## Error Handling

- No cookie → `/login`.
- Unknown key in POST → ignored (only EDITABLE_KEYS processed).
- Invalid value → inline error, nothing saved.
- DB unavailable for a `get` → fall back to the env default (wrap in try/except, return default) so the app and watcher keep working.

## Testing

- **`settings/core.py`**: `get` returns default when unset, DB value when set; `get_int`/`get_bool` parse correctly; `set_setting` upserts and rejects unknown keys; `all_settings` reports default + is_overridden + applies label.
- **`/settings`**: redirect without cookie; GET renders current values + defaults; POST persists valid values; POST with `watch_subfolder="../etc"` or negative int re-renders with an error and saves nothing.
- **upload**: with `max_upload_mb` set to a small value in the DB, an over-limit upload returns 413 (proves the live read).
- **watcher**: `_resolve_space_id` honors a `watch_space` setting over the env default (unit test with a stubbed/seeded setting).

## Implementation Order (for the plan)

1. `settings` table + conftest TRUNCATE.
2. `settings/core.py` with tests.
3. `webui/routes/settings.py` + template + nav, with tests.
4. Wire-ins: upload `max_upload_mb` (live) + test; watcher `watch_space` (live) + test; `webui/app.py` lifespan reads settings at startup.
5. Full suite + live smoke + README settings note.

## Risks

- `config` values are read via `DEFAULTS` lambdas at call time so a `config` reload in tests is honored (mirrors the `synthesis.generate` dynamic-read fix).
- Lifespan reading the DB at startup adds a DB dependency to boot; wrap in try/except so a transient DB hiccup falls back to env defaults rather than failing startup.
