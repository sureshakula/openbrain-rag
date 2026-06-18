"""Chat: persisted RAG conversations over the scoped document store."""
from __future__ import annotations
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from db.connection import get_conn
from accounts.core import accessible_space_ids
from retrieval.search import search, SearchFilters
from chat.core import (create_conversation, add_message, list_conversations,
                       get_messages, conversation_owner, set_title)
from chat.answer import answer
from webui.app import render
from webui.routes.auth import current_user

router = APIRouter()


def _require(request: Request):
    user = current_user(request)
    if user is None:
        return None, RedirectResponse(url="/login", status_code=307)
    return user, None


@router.get("/chat", response_class=HTMLResponse)
async def chat_home(request: Request):
    user, redirect = _require(request)
    if redirect:
        return redirect
    with get_conn() as conn:
        convs = list_conversations(conn, user["id"])
    return render(request, "chat.html",
                  {"page": "chat", "user": user, "conversations": convs,
                   "active": None, "messages": []})


@router.get("/chat/{conv_id}", response_class=HTMLResponse)
async def chat_view(request: Request, conv_id: int):
    user, redirect = _require(request)
    if redirect:
        return redirect
    with get_conn() as conn:
        if conversation_owner(conn, conv_id) != user["id"]:
            return HTMLResponse("Not found", status_code=404)
        convs = list_conversations(conn, user["id"])
        messages = get_messages(conn, conv_id)
    return render(request, "chat.html",
                  {"page": "chat", "user": user, "conversations": convs,
                   "active": conv_id, "messages": messages})


@router.post("/chat/new")
async def chat_new(request: Request):
    user, redirect = _require(request)
    if redirect:
        return redirect
    with get_conn() as conn:
        conv_id = create_conversation(conn, user["id"])
    return RedirectResponse(url=f"/chat/{conv_id}", status_code=303)


@router.post("/chat/{conv_id}/message", response_class=HTMLResponse)
async def chat_message(request: Request, conv_id: int, question: str = Form(...)):
    user, redirect = _require(request)
    if redirect:
        return redirect
    with get_conn() as conn:
        if conversation_owner(conn, conv_id) != user["id"]:
            return HTMLResponse("Not found", status_code=404)
        space_ids = accessible_space_ids(conn, user["id"])
        prior = get_messages(conn, conv_id)
        if not prior:
            set_title(conn, conv_id, question)
        add_message(conn, conv_id, "user", question)

    chunks = search(question, filters=SearchFilters(space_ids=space_ids))
    history = [{"role": m["role"], "content": m["content"]} for m in prior]
    try:
        body, citations = answer(question, chunks, history)
        cite_rows = [{"citation_index": c.citation_index, "chunk_id": c.chunk_id}
                     for c in citations]
    except Exception:
        body, cite_rows = "Sorry — generation failed, please try again.", []

    with get_conn() as conn:
        add_message(conn, conv_id, "assistant", body, citations=cite_rows)
        messages = get_messages(conn, conv_id)

    return render(request, "_chat_messages.html", {"messages": messages[-2:]})
