"""Drafts list + detail + accept/reject/abandon actions."""
from __future__ import annotations
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from db.connection import get_conn
from publish.bookstack_stub import publish
from webui.app import get_templates

router = APIRouter()


def _all_drafts() -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, topic, doc_type, status, created_at, reviewed_at
                  FROM drafts
                 ORDER BY status='pending' DESC, created_at DESC
            """)
            cols = [c.name for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def _get_draft(draft_id: int) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, topic, doc_type, body_markdown, status, feedback,
                          created_at, reviewed_at
                     FROM drafts WHERE id = %s""",
                (draft_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [c.name for c in cur.description]
            draft = dict(zip(cols, row))
            cur.execute(
                """SELECT dc.citation_index, dc.chunk_id, d.title AS document_title
                     FROM draft_citations dc
                     JOIN chunks c    ON c.id = dc.chunk_id
                     JOIN documents d ON d.id = c.document_id
                    WHERE dc.draft_id = %s
                    ORDER BY dc.citation_index""",
                (draft_id,),
            )
            ccols = [c.name for c in cur.description]
            draft["citations"] = [dict(zip(ccols, r)) for r in cur.fetchall()]
            return draft


@router.get("/drafts", response_class=HTMLResponse)
async def drafts_list(request: Request):
    return get_templates().TemplateResponse(
        request, "drafts.html",
        {"page": "drafts", "drafts": _all_drafts()},
    )


@router.get("/drafts/{draft_id}", response_class=HTMLResponse)
async def draft_detail(request: Request, draft_id: int):
    draft = _get_draft(draft_id)
    if not draft:
        raise HTTPException(404, "draft not found")
    return get_templates().TemplateResponse(
        request, "_draft_detail.html",
        {"draft": draft},
    )


@router.get("/drafts/{draft_id}/reject-form", response_class=HTMLResponse)
async def draft_reject_form(request: Request, draft_id: int):
    return get_templates().TemplateResponse(
        request, "_reject_form.html",
        {"draft_id": draft_id},
    )


@router.post("/drafts/{draft_id}/accept", response_class=HTMLResponse)
async def draft_accept(request: Request, draft_id: int):
    draft = _get_draft(draft_id)
    if not draft:
        raise HTTPException(404, "draft not found")
    publish(
        title=draft["topic"],
        body_markdown=draft["body_markdown"],
        source_refs=[str(c["document_title"]) for c in draft["citations"]],
    )
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE drafts SET status='accepted', reviewed_at=NOW() WHERE id=%s",
                (draft_id,),
            )
    return HTMLResponse('<div class="status indexed">accepted</div>')


@router.post("/drafts/{draft_id}/reject", response_class=HTMLResponse)
async def draft_reject(request: Request, draft_id: int,
                       feedback: Annotated[str, Form()] = ""):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE drafts SET status='rejected', feedback=%s, reviewed_at=NOW() WHERE id=%s",
                (feedback, draft_id),
            )
    return HTMLResponse('<div class="status failed">rejected</div>')


@router.post("/drafts/{draft_id}/abandon", response_class=HTMLResponse)
async def draft_abandon(request: Request, draft_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE drafts SET status='abandoned', reviewed_at=NOW() WHERE id=%s",
                (draft_id,),
            )
    return HTMLResponse('<div class="status">abandoned</div>')
