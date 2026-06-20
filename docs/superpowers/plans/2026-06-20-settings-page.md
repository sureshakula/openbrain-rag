# Settings Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `/settings` page to edit watch + ingestion settings at runtime, stored in a DB `settings` table that overrides env/`config.py` defaults.

**Architecture:** A `settings` key/value table + `settings/core.py` typed accessor with an env-fallback defaults map (read via lambdas at call time). Settings are read at their natural runtime points: upload size limit and watcher target space apply live; watcher dir/debounce/enable and worker count are wired at app startup (restart to apply). The watch folder is a subfolder under the mounted watch dir.

**Tech Stack:** Python 3.11, FastAPI + Jinja + HTMX, Postgres, psycopg2, pytest.

---

## Environment / conventions (all tasks)
- Postgres is remote. Tests run from the Mac venv against the remote test DB:
  ```
  source .venv/bin/activate
  VB_TEST_DB=openbrain_test VB_DB_HOST=192.168.1.83 VB_DB_PORT=5434 VB_DB_NAME=openbrain_test VB_DB_USER=postgres VB_DB_PASSWORD=postgres python -m pytest <args> -q
  ```
- `tests/conftest.py` applies `db/schema.sql` each session; the `db` fixture (RealDictCursor) truncates per test.
- Helpers shared with the webui (`get_conn()` uses tuple cursors) must open cursors explicitly with `RealDictCursor` (mirror `accounts/core.py`).
- `config.py` constants: `MAX_UPLOAD_MB`, `INGEST_WORKER_COUNT`, `WATCH_DIR`, `WATCH_DEBOUNCE_SEC`, `WATCH_SPACE`.
- Commit locally after each task; do NOT push (controller handles push).

## File Structure
- **Create** `settings/__init__.py` (empty), `settings/core.py` — typed settings accessor.
- **Modify** `db/schema.sql` — `settings` table.
- **Modify** `tests/conftest.py` — TRUNCATE list.
- **Create** `tests/settings/__init__.py` (empty), `tests/settings/test_core.py`.
- **Create** `webui/routes/settings.py`, `webui/templates/settings.html`.
- **Modify** `webui/app.py` (register router + lifespan reads settings), `webui/templates/base.html` (nav link).
- **Create** `tests/webui/test_settings.py`.
- **Modify** `webui/routes/upload.py` (live `max_upload_mb`), `webui/workers/folder_watcher.py` (live `watch_space`).
- **Modify** `tests/webui/test_upload.py`, `tests/workers/test_folder_watcher.py` (wire-in tests).
- **Modify** `README.md`.

NOTE: `settings` is both a stdlib-shadowing-ish name and our package — but Python resolves the local top-level package `settings/` fine since the repo root is on `sys.path`. There is no stdlib `settings` module, so no conflict.

---

## Task 1: `settings` table

**Files:**
- Modify: `db/schema.sql` (append after the CHAT block)
- Modify: `tests/conftest.py`

- [ ] **Step 1: Append to `db/schema.sql`:**
```sql
-- ─────────────────────────────────────────
-- SETTINGS (runtime key/value overrides)
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

- [ ] **Step 2: Add `settings` to the `db` fixture TRUNCATE in `tests/conftest.py`** (no FKs; add near the front):
```python
        cur.execute("""
            TRUNCATE settings, message_citations, messages, conversations,
                     draft_citations, drafts, review_queue, synthesis_runs,
                     knowledge_base, chunks, documents,
                     space_members, spaces, users RESTART IDENTITY CASCADE;
        """)
```

- [ ] **Step 3: Verify** — drop+reload remote test schema, run accounts tests:
```bash
source .venv/bin/activate
VB_DB_HOST=192.168.1.83 VB_DB_PORT=5434 VB_DB_NAME=openbrain_test VB_DB_USER=postgres VB_DB_PASSWORD=postgres python -c "import psycopg2; c=psycopg2.connect(host='192.168.1.83',port=5434,dbname='openbrain_test',user='postgres',password='postgres'); c.autocommit=True; c.cursor().execute('DROP SCHEMA public CASCADE; CREATE SCHEMA public;'); print('reset')"
VB_TEST_DB=openbrain_test VB_DB_HOST=192.168.1.83 VB_DB_PORT=5434 VB_DB_NAME=openbrain_test VB_DB_USER=postgres VB_DB_PASSWORD=postgres python -m pytest tests/accounts/test_core.py -q
```
Expected: accounts tests pass.

- [ ] **Step 4: Commit:**
```bash
git add db/schema.sql tests/conftest.py
git commit -m "feat(settings): settings key/value table"
```

---

## Task 2: `settings/core.py` accessor

**Files:**
- Create: `settings/__init__.py` (empty), `settings/core.py`
- Create: `tests/settings/__init__.py` (empty), `tests/settings/test_core.py`

- [ ] **Step 1: Write failing tests** `tests/settings/test_core.py`:
```python
import pytest
import settings.core as sc


def test_get_returns_default_when_unset(db):
    # max_upload_mb default comes from config.MAX_UPLOAD_MB (500)
    assert sc.get_int(db, "max_upload_mb") == 500


def test_set_and_get(db):
    sc.set_setting(db, "max_upload_mb", "50"); db.commit()
    assert sc.get(db, "max_upload_mb") == "50"
    assert sc.get_int(db, "max_upload_mb") == 50


def test_get_bool(db):
    sc.set_setting(db, "watch_enabled", "true"); db.commit()
    assert sc.get_bool(db, "watch_enabled") is True
    sc.set_setting(db, "watch_enabled", "false"); db.commit()
    assert sc.get_bool(db, "watch_enabled") is False


def test_set_rejects_unknown_key(db):
    with pytest.raises(ValueError):
        sc.set_setting(db, "not_a_real_key", "x")


def test_all_settings_reports_default_and_override(db):
    sc.set_setting(db, "watch_space", "Engineering"); db.commit()
    rows = {r["key"]: r for r in sc.all_settings(db)}
    assert set(rows) == set(sc.EDITABLE_KEYS)
    assert rows["watch_space"]["value"] == "Engineering"
    assert rows["watch_space"]["is_overridden"] is True
    assert rows["max_upload_mb"]["is_overridden"] is False
    assert rows["max_upload_mb"]["applies"] == "live"
    assert rows["watch_debounce_sec"]["applies"] == "restart"
```

- [ ] **Step 2: Run, confirm FAIL** (`settings.core` missing).

- [ ] **Step 3: Create `settings/__init__.py` (empty), `tests/settings/__init__.py` (empty), and `settings/core.py`:**
```python
"""Runtime settings: DB key/value overrides over env/config defaults."""
from __future__ import annotations
from psycopg2.extras import RealDictCursor

import config

# default is a 0-arg lambda read at call time so test config reloads are honored
DEFAULTS = {
    "watch_enabled":       lambda: bool(config.WATCH_DIR),
    "watch_subfolder":     lambda: "",
    "watch_debounce_sec":  lambda: config.WATCH_DEBOUNCE_SEC,
    "watch_space":         lambda: config.WATCH_SPACE,
    "ingest_worker_count": lambda: config.INGEST_WORKER_COUNT,
    "max_upload_mb":       lambda: config.MAX_UPLOAD_MB,
}
EDITABLE_KEYS = list(DEFAULTS)
APPLIES = {
    "watch_space": "live", "max_upload_mb": "live",
    "watch_enabled": "restart", "watch_subfolder": "restart",
    "watch_debounce_sec": "restart", "ingest_worker_count": "restart",
}

_TRUE = {"1", "true", "yes", "on"}


def _default_str(key: str) -> str:
    return str(DEFAULTS[key]())


def get(conn, key: str) -> str:
    """DB value if set, else the env/config default (as str). Falls back to the
    default on any DB error so callers never crash on a settings lookup."""
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
            row = cur.fetchone()
            if row is not None:
                return row["value"]
    except Exception:
        pass
    return _default_str(key)


def get_int(conn, key: str) -> int:
    try:
        return int(get(conn, key))
    except (ValueError, TypeError):
        return int(DEFAULTS[key]())


def get_bool(conn, key: str) -> bool:
    return get(conn, key).strip().lower() in _TRUE


def set_setting(conn, key: str, value: str) -> None:
    if key not in DEFAULTS:
        raise ValueError(f"unknown setting: {key}")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO settings (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (key, str(value)),
        )


def all_settings(conn) -> list[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT key, value FROM settings")
        overrides = {r["key"]: r["value"] for r in cur.fetchall()}
    out = []
    for key in EDITABLE_KEYS:
        default = _default_str(key)
        out.append({
            "key": key,
            "value": overrides.get(key, default),
            "default": default,
            "is_overridden": key in overrides,
            "applies": APPLIES[key],
        })
    return out
```

- [ ] **Step 4: Run, confirm PASS:** `... python -m pytest tests/settings/test_core.py -v`

- [ ] **Step 5: Commit:**
```bash
git add settings/__init__.py settings/core.py tests/settings/__init__.py tests/settings/test_core.py
git commit -m "feat(settings): typed settings accessor (settings/core.py)"
```

---

## Task 3: `/settings` page + route

**Files:**
- Create: `webui/routes/settings.py`, `webui/templates/settings.html`
- Modify: `webui/app.py` (register router), `webui/templates/base.html` (nav)
- Test: `tests/webui/test_settings.py`

- [ ] **Step 1: Write failing tests** `tests/webui/test_settings.py`:
```python
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
```

- [ ] **Step 2: Run, confirm FAIL** (no /settings route).

- [ ] **Step 3: Create `webui/routes/settings.py`:**
```python
"""Settings: edit watch + ingestion runtime overrides."""
from __future__ import annotations
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from db.connection import get_conn
import settings.core as sc
from webui.app import render
from webui.routes.auth import current_user

router = APIRouter()


def _validate(form: dict) -> str | None:
    """Return an error message, or None if valid."""
    sub = (form.get("watch_subfolder") or "").strip()
    if ".." in sub or sub.startswith("/") or "\\" in sub:
        return "invalid watch_subfolder: must be a relative path under the watch dir"
    for key, minimum in (("watch_debounce_sec", 0),
                         ("ingest_worker_count", 1),
                         ("max_upload_mb", 1)):
        try:
            if int(form.get(key, "")) < minimum:
                return f"invalid {key}: must be >= {minimum}"
        except (ValueError, TypeError):
            return f"invalid {key}: must be an integer"
    return None


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, saved: str | None = None,
                        error: str | None = None):
    user = current_user(request)
    if user is None:
        return RedirectResponse(url="/login", status_code=307)
    with get_conn() as conn:
        rows = sc.all_settings(conn)
    return render(request, "settings.html",
                  {"page": "settings", "settings": rows,
                   "saved": saved, "error": error})


@router.post("/settings", response_class=HTMLResponse)
async def settings_save(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse(url="/login", status_code=307)
    form = dict(await request.form())
    # checkbox: present means true
    form["watch_enabled"] = "true" if form.get("watch_enabled") in ("true", "on", "1") else "false"
    err = _validate(form)
    if err:
        with get_conn() as conn:
            rows = sc.all_settings(conn)
        return render(request, "settings.html",
                      {"page": "settings", "settings": rows, "error": err})
    with get_conn() as conn:
        for key in sc.EDITABLE_KEYS:
            if key in form:
                sc.set_setting(conn, key, form[key])
    return RedirectResponse(url="/settings?saved=1", status_code=303)
```

- [ ] **Step 4: Create `webui/templates/settings.html`** (READ `base.html` first to match block names + classes):
```html
{% extends "base.html" %}
{% block title %}Settings — OpenBrain{% endblock %}
{% block content %}
<div class="toolbar"><h2>Settings</h2></div>
{% if saved %}<p class="status indexed">Saved.</p>{% endif %}
{% if error %}<p style="color: var(--danger, #c00)">{{ error }}</p>{% endif %}
<p style="color: var(--muted); font-size: 12px;">
  Settings marked <strong>restart</strong> take effect after an app restart;
  <strong>live</strong> settings apply immediately.
</p>
<form method="post" action="/settings">
  <table>
    <thead><tr><th>Setting</th><th>Value</th><th>Default</th><th>Applies</th></tr></thead>
    <tbody>
    {% for s in settings %}
      <tr>
        <td>{{ s.key }}</td>
        <td>
          {% if s.key == 'watch_enabled' %}
            <input type="checkbox" name="watch_enabled" value="true" {% if s.value|lower in ['true','1','yes','on'] %}checked{% endif %}>
          {% else %}
            <input class="input" type="text" name="{{ s.key }}" value="{{ s.value }}">
          {% endif %}
        </td>
        <td style="color: var(--muted)">{{ s.default }}</td>
        <td style="color: var(--muted)">{{ s.applies }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  <button class="btn primary" type="submit" style="margin-top:12px;">Save</button>
</form>
{% endblock %}
```

- [ ] **Step 5: Register the router in `webui/app.py`** — add `settings` to the import + include block in `create_app`:
```python
    from webui.routes import auth, upload, inventory, docgen, drafts, spaces, chat, settings
    ...
    app.include_router(settings.router)
```
IMPORTANT: this imports the route module `webui.routes.settings`, NOT the top-level `settings` package. To avoid ambiguity, in `webui/app.py` import as: `from webui.routes import (auth, upload, inventory, docgen, drafts, spaces, chat, settings as settings_routes)` and `app.include_router(settings_routes.router)`. (The route file itself imports the accessor as `import settings.core as sc`, which is unambiguous.)

- [ ] **Step 6: Add a "Settings" nav link in `webui/templates/base.html`** — READ the file, add `<a href="/settings" ...>Settings</a>` matching the existing nav pattern (including any `{% if page == 'settings' %}active{% endif %}`).

- [ ] **Step 7: Run, confirm PASS:** `... python -m pytest tests/webui/test_settings.py -v` (5 passed).

- [ ] **Step 8: Commit:**
```bash
git add webui/routes/settings.py webui/templates/settings.html webui/app.py webui/templates/base.html tests/webui/test_settings.py
git commit -m "feat(settings): /settings page (watch + ingestion)"
```

---

## Task 4: Wire-ins — live upload limit + live watch space

**Files:**
- Modify: `webui/routes/upload.py`
- Modify: `webui/workers/folder_watcher.py`
- Test: `tests/webui/test_upload.py`, `tests/workers/test_folder_watcher.py`

- [ ] **Step 1: Write failing tests.**
Add to `tests/webui/test_upload.py` (the client fixture sets `ob_user`; adapt to its existing fixtures):
```python
async def test_upload_honors_max_upload_mb_setting(client, db, tmp_upload_dir):
    import settings.core as sc
    sc.set_setting(db, "max_upload_mb", "0"); db.commit()  # 0 MB -> any file too big
    files = {"file": ("big.md", b"hello world", "text/markdown")}
    r = await client.post("/upload", data={"space": ""}, files=files)
    assert r.status_code == 413
```
Add to `tests/workers/test_folder_watcher.py`:
```python
def test_resolve_space_id_uses_setting(db):
    import settings.core as sc
    from accounts.core import create_shared_space, get_or_create_user
    from webui.workers.folder_watcher import FolderWatcher
    u = get_or_create_user(db, "watchcfg")
    sp = create_shared_space(db, "WatchTarget", u["id"]); db.commit()
    sc.set_setting(db, "watch_space", "WatchTarget"); db.commit()
    assert FolderWatcher._resolve_space_id(db) == sp["id"]
```

- [ ] **Step 2: Run, confirm FAIL.**
```
... python -m pytest tests/webui/test_upload.py::test_upload_honors_max_upload_mb_setting tests/workers/test_folder_watcher.py::test_resolve_space_id_uses_setting -v
```

- [ ] **Step 3: Wire `webui/routes/upload.py`.** Add `import settings.core as sc` at the top. Replace the size-limit block:
```python
    with get_conn() as conn:
        max_mb = sc.get_int(conn, "max_upload_mb")
    max_bytes = max_mb * 1024 * 1024
    data = await file.read()
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail=f"file exceeds {max_mb} MB")
```
(Replaces the previous `config.MAX_UPLOAD_MB` reads on both the limit and the message.)

- [ ] **Step 4: Wire `webui/workers/folder_watcher.py`.** Add `import settings.core as sc` at the top. In `_resolve_space_id(conn)`, change the line that reads the watch space from `config.WATCH_SPACE` to the setting:
```python
        watch_space = (sc.get(conn, "watch_space") or "").strip()
```
Leave the rest of `_resolve_space_id` (Common fallback, `space_by_name` lookup, warning log) unchanged.

- [ ] **Step 5: Run, confirm PASS:**
```
... python -m pytest tests/webui/test_upload.py tests/workers/test_folder_watcher.py -v
```

- [ ] **Step 6: Commit:**
```bash
git add webui/routes/upload.py webui/workers/folder_watcher.py tests/webui/test_upload.py tests/workers/test_folder_watcher.py
git commit -m "feat(settings): live max_upload_mb + watch_space from settings"
```

---

## Task 5: Lifespan reads settings at startup + verify + docs

**Files:**
- Modify: `webui/app.py` (lifespan)
- Modify: `README.md`

- [ ] **Step 1: Update the lifespan in `webui/app.py`** to read watch + worker settings from the DB at startup (env fallback via `settings.core`). The current lifespan imports `INGEST_WORKER_COUNT, WATCH_DIR, WATCH_DEBOUNCE_SEC` from config and starts the queue + watcher. Replace its body so it reads settings:
```python
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        import settings.core as sc
        from db.connection import get_conn
        watcher: FolderWatcher | None = None
        if start_workers:
            # read runtime settings (fall back to env on any error)
            try:
                with get_conn() as conn:
                    worker_count = sc.get_int(conn, "ingest_worker_count")
                    watch_enabled = sc.get_bool(conn, "watch_enabled")
                    watch_subfolder = sc.get(conn, "watch_subfolder")
                    debounce = sc.get_int(conn, "watch_debounce_sec")
            except Exception:
                worker_count = INGEST_WORKER_COUNT
                watch_enabled = bool(WATCH_DIR)
                watch_subfolder = ""
                debounce = WATCH_DEBOUNCE_SEC
            await queue.start(worker_count=worker_count)
            log.info("ingest queue started with %d workers", worker_count)
            if watch_enabled and WATCH_DIR:
                watch_path = Path(WATCH_DIR) / watch_subfolder if watch_subfolder else Path(WATCH_DIR)
                if watch_path.is_dir():
                    watcher = FolderWatcher(watch_path, queue, debounce_sec=debounce)
                    await watcher.start()
                else:
                    log.warning("watch path %s is not a directory; watcher disabled", watch_path)
        try:
            yield {"queue": queue}
        finally:
            if watcher:
                await watcher.stop()
            if start_workers:
                await queue.stop()
```
Keep the existing imports of `INGEST_WORKER_COUNT, WATCH_DIR, WATCH_DEBOUNCE_SEC` (used as fallbacks).

- [ ] **Step 2: Drop + reload test schema, run the WHOLE suite:**
```bash
source .venv/bin/activate
VB_DB_HOST=192.168.1.83 VB_DB_PORT=5434 VB_DB_NAME=openbrain_test VB_DB_USER=postgres VB_DB_PASSWORD=postgres python -c "import psycopg2; c=psycopg2.connect(host='192.168.1.83',port=5434,dbname='openbrain_test',user='postgres',password='postgres'); c.autocommit=True; c.cursor().execute('DROP SCHEMA public CASCADE; CREATE SCHEMA public;'); print('reset')"
VB_TEST_DB=openbrain_test VB_DB_HOST=192.168.1.83 VB_DB_PORT=5434 VB_DB_NAME=openbrain_test VB_DB_USER=postgres VB_DB_PASSWORD=postgres python -m pytest -q
```
Expected: all pass (start_workers defaults to False in the test app factory, so the lifespan watcher block is not exercised by the suite; the smoke e2e test uses `start_workers=True` via its own lifespan context and must still pass — it has no settings rows, so all defaults apply).

- [ ] **Step 3: Add a "Settings" entry to the `/settings` row of the README Webui pages table** (after the `/spaces` row added earlier):
```
| `/settings` | Edit watch + ingestion settings at runtime (watch folder/debounce/space/enable, worker count, max upload). Live for upload-limit + watch-space; others apply after an app restart. |
```

- [ ] **Step 4: Commit:**
```bash
git add webui/app.py README.md
git commit -m "feat(settings): lifespan reads watch/worker settings at startup; docs"
```

---

## Self-Review

**Spec coverage:**
- `settings` table + conftest → Task 1. ✅
- `settings/core.py` (get/get_int/get_bool/set_setting/all_settings, DEFAULTS lambdas, EDITABLE_KEYS, APPLIES) → Task 2. ✅
- `/settings` page (GET render, POST validate+save, auth-gate, subfolder/int validation) + nav → Task 3. ✅
- Wire-ins: live `max_upload_mb` (upload), live `watch_space` (watcher) → Task 4. ✅
- Lifespan reads `watch_enabled`/`watch_subfolder`/`watch_debounce_sec`/`ingest_worker_count` at startup, env fallback, subfolder-under-mount path → Task 5. ✅
- Docs → Task 5. ✅
- Testing (core, page auth/render/save/validation, upload live limit, watcher live space) → covered. ✅

**Placeholder scan:** none — all steps have full code.

**Type/name consistency:** `sc.get/get_int/get_bool/set_setting/all_settings`, `EDITABLE_KEYS`, `APPLIES`, `DEFAULTS` used identically in Tasks 2–5. The `settings` package vs `webui.routes.settings` ambiguity is resolved explicitly (import the route module as `settings_routes` in `app.py`; the accessor is always `import settings.core as sc`). `_resolve_space_id(conn)` signature unchanged (Task 4 only changes one line inside it).

**Risk:** Task 5's lifespan change runs only when `start_workers=True` (production + the e2e smoke test). The smoke test has no settings rows → defaults → behaves as before. The `get_conn()` at startup is wrapped in try/except → DB hiccup falls back to env defaults, not a boot failure.
