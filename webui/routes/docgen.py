"""Doc-gen: form GET + generate POST returning HTMX partial."""
from __future__ import annotations
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from psycopg2.extras import Json

from db.connection import get_conn
from accounts.core import accessible_space_ids
from retrieval.search import search, SearchFilters
from synthesis.generate import generate, DOC_TYPES
from webui.app import render
from webui.routes.auth import current_user

router = APIRouter()


@router.get("/docgen", response_class=HTMLResponse)
async def docgen_form(request: Request):
    return render(
        request, "docgen.html",
        {"page": "docgen", "doc_types": DOC_TYPES},
    )


@router.post("/docgen/generate", response_class=HTMLResponse)
async def docgen_generate(
    request: Request,
    topic: Annotated[str, Form()],
    doc_type: Annotated[str, Form()] = "process",
    source_types: Annotated[str, Form()] = "",
    file_extensions: Annotated[str, Form()] = "",
):
    user = current_user(request)
    if user is None:
        return RedirectResponse(url="/login", status_code=307)
    with get_conn() as conn:
        space_ids = accessible_space_ids(conn, user["id"])
    filters = SearchFilters(
        source_types=[s for s in source_types.split(",") if s],
        file_extensions=[e for e in file_extensions.split(",") if e],
        space_ids=space_ids,
    )
    chunks = search(topic, filters=filters)
    body, citations = generate(topic, doc_type, chunks)

    source_filter_json = {
        "source_types": filters.source_types,
        "file_extensions": filters.file_extensions,
    }

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO drafts (topic, doc_type, body_markdown, source_filter, status)
                   VALUES (%s, %s, %s, %s, 'pending') RETURNING id""",
                (topic, doc_type, body, Json(source_filter_json)),
            )
            draft_id = cur.fetchone()[0]
            for c in citations:
                cur.execute(
                    """INSERT INTO draft_citations (draft_id, chunk_id, citation_index)
                       VALUES (%s, %s, %s)""",
                    (draft_id, c.chunk_id, c.citation_index),
                )

    return render(
        request, "_docgen_draft.html",
        {"draft_id": draft_id, "body": body, "citations": citations},
    )
