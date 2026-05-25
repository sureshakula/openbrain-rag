"""Upload route: GET form, POST multipart -> enqueue."""
from __future__ import annotations
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

import config
from db.connection import get_conn
from ingestion.core import DEFAULT_NAMESPACE, SUGGESTED_NAMESPACES, insert_document, sha256_text
from webui.app import render

router = APIRouter()


@router.get("/upload", response_class=HTMLResponse)
async def upload_form(request: Request):
    return render(
        request, "upload.html",
        {"page": "upload", "namespaces": SUGGESTED_NAMESPACES,
         "default_namespace": DEFAULT_NAMESPACE},
    )


@router.post("/upload")
async def upload_post(request: Request,
                      file: UploadFile = File(...),
                      namespace: str = Form(DEFAULT_NAMESPACE)):
    max_bytes = config.MAX_UPLOAD_MB * 1024 * 1024
    data = await file.read()
    if len(data) > max_bytes:
        raise HTTPException(status_code=413,
                            detail=f"file exceeds {config.MAX_UPLOAD_MB} MB")

    upload_dir = Path(config.UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)
    content_hash = sha256_text(data.decode("utf-8", errors="replace"))
    ext = Path(file.filename or "").suffix.lower()
    saved_path = upload_dir / f"{content_hash}{ext}"
    saved_path.write_bytes(data)

    with get_conn() as conn:
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
            namespace=(namespace or DEFAULT_NAMESPACE).strip().lower(),
            metadata={"original_filename": file.filename},
        )

    queue = request.app.state.queue
    await queue.enqueue(doc_id, saved_path)

    return RedirectResponse(url="/inventory", status_code=303)
