"""Upload route: GET form, POST multipart -> enqueue."""
from __future__ import annotations
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

import config
import settings.core as sc
from accounts.core import accessible_space_ids, ensure_common_space, list_spaces_for
from db.connection import get_conn
from ingestion.core import insert_document, sha256_text
from webui.app import render
from webui.routes.auth import current_user

router = APIRouter()


@router.get("/upload", response_class=HTMLResponse)
async def upload_form(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse(url="/login", status_code=307)
    with get_conn() as conn:
        spaces = list_spaces_for(conn, user["id"])
    return render(
        request, "upload.html",
        {"page": "upload", "spaces": spaces},
    )


@router.post("/upload")
async def upload_post(request: Request,
                      file: UploadFile = File(...),
                      space: str = Form("")):
    user = current_user(request)
    if user is None:
        return RedirectResponse(url="/login", status_code=307)

    with get_conn() as conn:
        max_mb = sc.get_int(conn, "max_upload_mb")
    max_bytes = max_mb * 1024 * 1024
    data = await file.read()
    if len(data) > max_bytes:
        raise HTTPException(status_code=413,
                            detail=f"file exceeds {max_mb} MB")

    with get_conn() as conn:
        # Resolve the target space id
        allowed = accessible_space_ids(conn, user["id"])
        common_id = ensure_common_space(conn)

        space_id: int
        try:
            requested = int(space)
            space_id = requested if requested in allowed else common_id
        except (ValueError, TypeError):
            space_id = common_id

        upload_dir = Path(config.UPLOAD_DIR)
        upload_dir.mkdir(parents=True, exist_ok=True)
        content_hash = sha256_text(data.decode("utf-8", errors="replace"))
        ext = Path(file.filename or "").suffix.lower()
        saved_path = upload_dir / f"{content_hash}{ext}"
        saved_path.write_bytes(data)

        doc_id = insert_document(
            conn,
            source_type="web_ui",
            source_ref=str(saved_path),
            title=Path(file.filename or "untitled").stem,
            raw_content="",
            content_hash=content_hash,
            file_size=len(data),
            file_extension=ext,
            status="queued",
            space_id=space_id,
            created_by=user["id"],
            metadata={"original_filename": file.filename},
        )

    queue = request.app.state.queue
    await queue.enqueue(doc_id, saved_path)

    return RedirectResponse(url="/inventory", status_code=303)
