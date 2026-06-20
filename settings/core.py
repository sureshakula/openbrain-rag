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
