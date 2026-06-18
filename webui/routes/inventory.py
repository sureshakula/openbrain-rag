"""Inventory list + HTMX-polled partial (scoped to the current user)."""
from __future__ import annotations
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from db.connection import get_conn
from accounts.core import accessible_space_ids, list_spaces_for
from webui.app import render
from webui.routes.auth import current_user

router = APIRouter()


def _query_docs(*, space_ids, status, source, ftype, space, search, limit=200):
    where = ["active = TRUE", "space_id = ANY(%s)"]
    args = [space_ids]
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


def _counts(space_ids):
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
