"""Lightweight username identity (no password) for the trusted LAN."""
from __future__ import annotations
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from db.connection import get_conn
from accounts.core import get_or_create_user
from webui.app import render

router = APIRouter()

COOKIE = "ob_user"


def current_user(request: Request) -> dict | None:
    """Resolve the logged-in user from the cookie, or None."""
    username = request.cookies.get(COOKIE)
    if not username:
        return None
    with get_conn() as conn:
        return get_or_create_user(conn, username)


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return render(request, "login.html", {"page": "login"})


@router.post("/login")
async def login_submit(request: Request, username: str = Form(...)):
    with get_conn() as conn:
        user = get_or_create_user(conn, username)
    resp = RedirectResponse(url="/inventory", status_code=303)
    resp.set_cookie(COOKIE, user["username"], httponly=True, samesite="lax")
    return resp


@router.post("/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp
