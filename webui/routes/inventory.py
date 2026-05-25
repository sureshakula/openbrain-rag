"""Inventory list + HTMX-polled partial."""
from __future__ import annotations
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from db.connection import get_conn
from webui.app import get_templates

router = APIRouter()


def _query_docs(*, status: str | None, source: str | None, ftype: str | None,
                search: str | None, limit: int = 200) -> list[dict]:
    where: list[str] = ["active = TRUE"]
    args: list = []
    if status:
        where.append("status = %s"); args.append(status)
    if source:
        where.append("source_type = %s"); args.append(source)
    if ftype:
        where.append("file_extension = %s"); args.append(ftype)
    if search:
        where.append("title ILIKE %s"); args.append(f"%{search}%")
    sql = f"""
        SELECT d.id, d.title, d.source_type, d.file_extension, d.status,
               d.failure_reason, d.ingested_at,
               (SELECT COUNT(*) FROM chunks c WHERE c.document_id = d.id) AS chunk_count
          FROM documents d
         WHERE {' AND '.join(where)}
         ORDER BY d.ingested_at DESC NULLS LAST
         LIMIT {limit}
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            cols = [c.name for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def _counts() -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE active AND status = 'indexed')      AS indexed,
                    COUNT(*) FILTER (WHERE active AND status IN
                        ('queued','processing','transcribing'))                AS processing,
                    COUNT(*) FILTER (WHERE active AND status = 'failed')       AS failed
                FROM documents
            """)
            row = cur.fetchone()
            return {"indexed": row[0], "processing": row[1], "failed": row[2]}


@router.get("/inventory", response_class=HTMLResponse)
async def inventory(request: Request,
                    status: str | None = None,
                    source: str | None = None,
                    ftype: str | None = None,
                    search: str | None = None):
    return get_templates().TemplateResponse(
        request, "inventory.html",
        {"page": "inventory",
         "rows": _query_docs(status=status, source=source, ftype=ftype, search=search),
         "counts": _counts(),
         "filters": {"status": status or "", "source": source or "",
                     "ftype": ftype or "", "search": search or ""}},
    )


@router.get("/inventory/rows", response_class=HTMLResponse)
async def inventory_rows(request: Request,
                         status: str | None = None,
                         source: str | None = None,
                         ftype: str | None = None,
                         search: str | None = None):
    return get_templates().TemplateResponse(
        request, "_inventory_rows.html",
        {"rows": _query_docs(status=status, source=source, ftype=ftype, search=search)},
    )
