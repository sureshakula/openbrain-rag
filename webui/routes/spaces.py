"""Manage spaces: list accessible, create shared, join shared."""
from __future__ import annotations
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from db.connection import get_conn
from accounts.core import list_spaces_for, create_shared_space, join_space_by_name
from webui.app import render
from webui.routes.auth import current_user

router = APIRouter()


def _user_or_redirect(request: Request):
    user = current_user(request)
    if user is None:
        return None, RedirectResponse(url="/login", status_code=307)
    return user, None


@router.get("/spaces", response_class=HTMLResponse)
async def spaces_page(request: Request, error: str | None = None):
    user, redirect = _user_or_redirect(request)
    if redirect:
        return redirect
    with get_conn() as conn:
        spaces = list_spaces_for(conn, user["id"])
    return render(request, "spaces.html",
                  {"page": "spaces", "spaces": spaces, "user": user, "error": error})


@router.post("/spaces/create")
async def spaces_create(request: Request, name: str = Form(...)):
    user, redirect = _user_or_redirect(request)
    if redirect:
        return redirect
    try:
        with get_conn() as conn:
            create_shared_space(conn, name, user["id"])
    except ValueError as e:
        return RedirectResponse(url=f"/spaces?error={e}", status_code=303)
    return RedirectResponse(url="/spaces", status_code=303)


@router.post("/spaces/join")
async def spaces_join(request: Request, name: str = Form(...)):
    user, redirect = _user_or_redirect(request)
    if redirect:
        return redirect
    try:
        with get_conn() as conn:
            join_space_by_name(conn, name, user["id"])
    except ValueError as e:
        return RedirectResponse(url=f"/spaces?error={e}", status_code=303)
    return RedirectResponse(url="/spaces", status_code=303)
