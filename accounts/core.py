"""Users, spaces, membership — the app-layer access control source of truth."""
from __future__ import annotations
import secrets
from psycopg2.extras import RealDictCursor


def _norm(name: str) -> str:
    return (name or "").strip().lower()


def ensure_common_space(conn) -> int:
    """Return the Common shared space id, creating it if missing."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id FROM spaces WHERE kind='shared' AND lower(name)='common'"
        )
        row = cur.fetchone()
        if row:
            return row["id"]
        cur.execute(
            "INSERT INTO spaces (name, kind, created_by) "
            "VALUES ('Common','shared',NULL) RETURNING id"
        )
        return cur.fetchone()["id"]


def get_or_create_user(conn, username: str) -> dict:
    """Get-or-create a user by normalized username. Creates the user's personal
    space (owner membership) and joins Common. Returns {id, username, mcp_token}."""
    uname = _norm(username)
    if not uname:
        raise ValueError("username required")
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT id, username, mcp_token FROM users WHERE username=%s",
                    (uname,))
        row = cur.fetchone()
        if row:
            return {"id": row["id"], "username": row["username"], "mcp_token": row["mcp_token"]}
        token = secrets.token_urlsafe(24)
        cur.execute(
            "INSERT INTO users (username, mcp_token) VALUES (%s,%s) RETURNING id",
            (uname, token),
        )
        uid = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO spaces (name, kind, created_by) VALUES (%s,'personal',%s) "
            "RETURNING id", (uname, uid),
        )
        personal_id = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO space_members (space_id, user_id, role) VALUES (%s,%s,'owner')",
            (personal_id, uid),
        )
    common_id = ensure_common_space(conn)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "INSERT INTO space_members (space_id, user_id, role) VALUES (%s,%s,'member') "
            "ON CONFLICT DO NOTHING", (common_id, uid),
        )
    return {"id": uid, "username": uname, "mcp_token": token}


def user_by_token(conn, token: str) -> dict | None:
    if not token:
        return None
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT id, username, mcp_token FROM users WHERE mcp_token=%s",
                    (token,))
        row = cur.fetchone()
        if not row:
            return None
        return {"id": row["id"], "username": row["username"], "mcp_token": row["mcp_token"]}


def accessible_space_ids(conn, user_id: int) -> list[int]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT space_id FROM space_members WHERE user_id=%s", (user_id,))
        return [r["space_id"] for r in cur.fetchall()]


def create_shared_space(conn, name: str, owner_user_id: int) -> dict:
    nname = _norm(name)
    if not nname:
        raise ValueError("space name required")
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT 1 FROM spaces WHERE kind='shared' AND lower(name)=%s",
                    (nname,))
        if cur.fetchone():
            raise ValueError(f"shared space '{name}' already exists")
        cur.execute(
            "INSERT INTO spaces (name, kind, created_by) VALUES (%s,'shared',%s) "
            "RETURNING id, name, kind", (name.strip(), owner_user_id),
        )
        row = cur.fetchone()
        sid, sname, skind = row["id"], row["name"], row["kind"]
        cur.execute(
            "INSERT INTO space_members (space_id, user_id, role) VALUES (%s,%s,'owner')",
            (sid, owner_user_id),
        )
        return {"id": sid, "name": sname, "kind": skind}


def space_by_name(conn, name: str) -> dict | None:
    """Look up a shared space by name (case-insensitive)."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id, name, kind FROM spaces WHERE kind='shared' AND lower(name)=%s",
            (_norm(name),),
        )
        row = cur.fetchone()
        return {"id": row["id"], "name": row["name"], "kind": row["kind"]} if row else None


def join_space_by_name(conn, name: str, user_id: int) -> dict:
    sp = space_by_name(conn, name)
    if not sp:
        raise ValueError(f"shared space '{name}' not found")
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "INSERT INTO space_members (space_id, user_id, role) VALUES (%s,%s,'member') "
            "ON CONFLICT DO NOTHING", (sp["id"], user_id),
        )
    return sp


def list_spaces_for(conn, user_id: int) -> list[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
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
        return [dict(r) for r in cur.fetchall()]
