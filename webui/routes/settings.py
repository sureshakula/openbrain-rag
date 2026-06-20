"""Settings: edit watch + ingestion runtime overrides."""
from __future__ import annotations
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from db.connection import get_conn
import settings.core as sc
from webui.app import render
from webui.routes.auth import current_user

router = APIRouter()


def _validate(form: dict) -> str | None:
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
                  {"page": "settings", "settings": rows, "saved": saved, "error": error})


@router.post("/settings", response_class=HTMLResponse)
async def settings_save(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse(url="/login", status_code=307)
    form = dict(await request.form())
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
