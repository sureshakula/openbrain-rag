# Chat Interface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `/chat` RAG interface with persisted per-user conversations and cited, non-streaming answers grounded in space-scoped documents.

**Architecture:** Extract the shared Claude-call + citation logic from `synthesis/generate.py` into `synthesis/llm.py`. Add `conversations`/`messages`/`message_citations` tables, a `chat/core.py` store, a `chat/answer.py` RAG answerer, and a `webui/routes/chat.py` page. Retrieval is scoped to the user's accessible spaces; conversations are owned per-user.

**Tech Stack:** Python 3.11, FastAPI + Jinja + HTMX, Postgres + pgvector, psycopg2, httpx, pytest, respx.

---

## Environment / conventions (all tasks)
- Postgres is remote. Run tests from the Mac venv against the remote test DB:
  ```
  source .venv/bin/activate
  VB_TEST_DB=openbrain_test VB_DB_HOST=192.168.1.83 VB_DB_PORT=5434 VB_DB_NAME=openbrain_test VB_DB_USER=postgres VB_DB_PASSWORD=postgres python -m pytest <args> -q
  ```
- `tests/conftest.py` applies `db/schema.sql` to the test DB each session and the `db` fixture (RealDictCursor) truncates tables per test.
- The webui's `get_conn()` uses TUPLE cursors; the `db` test fixture uses RealDictCursor. Helpers shared between them must not depend on a specific cursor type — when reading rows in `chat/core.py`, open cursors explicitly with `RealDictCursor` (mirror `accounts/core.py`).
- `accounts/core.py`: `get_or_create_user`, `accessible_space_ids`. `retrieval.search`: `search(query, filters, top_k)`, `SearchFilters(space_ids=...)`, `Chunk`. `webui/routes/auth.py`: `current_user(request)`. `webui/app.py`: `create_app(*, start_workers=False)`, `render`.
- Commit locally after each task. Do NOT push (controller handles push).

## File Structure
- **Create** `synthesis/llm.py` — shared `Citation`, `call_claude`, `map_citations`.
- **Modify** `synthesis/generate.py` — use `synthesis.llm`; re-export `Citation`.
- **Modify** `db/schema.sql` — chat tables (after the spaces sections).
- **Modify** `tests/conftest.py` — TRUNCATE list.
- **Create** `chat/__init__.py`, `chat/core.py`, `chat/answer.py`.
- **Create** `tests/chat/__init__.py`, `tests/chat/test_core.py`, `tests/chat/test_answer.py`.
- **Create** `tests/synthesis/test_llm.py`.
- **Create** `webui/routes/chat.py`, `webui/templates/chat.html`, `webui/templates/_chat_messages.html`.
- **Modify** `webui/app.py` (register router), `webui/templates/base.html` (nav link).
- **Create** `tests/webui/test_chat.py`.
- **Modify** `README.md`.

---

## Task 1: Extract `synthesis/llm.py` (DRY refactor)

**Files:**
- Create: `synthesis/llm.py`
- Modify: `synthesis/generate.py`
- Test: `tests/synthesis/test_llm.py` (new), `tests/synthesis/test_generate.py` (must stay green)

- [ ] **Step 1: Write the failing test** `tests/synthesis/test_llm.py`:

```python
import respx
import httpx
from synthesis.llm import call_claude, map_citations, Citation
from retrieval.search import Chunk


def _chunk(cid, doc_id, title, content):
    return Chunk(chunk_id=cid, document_id=doc_id, document_title=title,
                 source_type="local_file", file_extension=".md",
                 content=content, rrf_score=0.5)


@respx.mock
def test_call_claude_returns_text():
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(200, json={
            "content": [{"type": "text", "text": "hello [1]"}],
            "model": "x", "usage": {"input_tokens": 1, "output_tokens": 1},
        })
    )
    out = call_claude("sys", "user prompt")
    assert out == "hello [1]"


def test_map_citations_maps_and_filters():
    chunks = [_chunk(101, 1, "a.md", "x"), _chunk(202, 2, "b.md", "y")]
    cites = map_citations("uses [1] and [2] and [9]", chunks)
    assert [c.chunk_id for c in cites] == [101, 202]
    assert all(isinstance(c, Citation) for c in cites)
    assert [c.citation_index for c in cites] == [1, 2]
```

- [ ] **Step 2: Run, confirm FAIL** (`synthesis.llm` does not exist):
`... python -m pytest tests/synthesis/test_llm.py -v`

- [ ] **Step 3: Create `synthesis/llm.py`:**

```python
"""Shared Claude call + citation mapping for synthesis and chat."""
from __future__ import annotations
import re
from dataclasses import dataclass

import httpx

import config  # read settings dynamically at call time
from retrieval.search import Chunk


@dataclass
class Citation:
    citation_index: int
    chunk_id: int
    document_id: int
    document_title: str


def call_claude(system: str, user_prompt: str) -> str:
    """POST to the Anthropic-compatible endpoint; return concatenated assistant text."""
    payload = {
        "model": config.SYNTHESIS_MODEL,
        "max_tokens": config.SYNTHESIS_MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    headers = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
    if config.ANTHROPIC_AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {config.ANTHROPIC_AUTH_TOKEN}"
    if config.ANTHROPIC_API_KEY:
        headers["x-api-key"] = config.ANTHROPIC_API_KEY
    url = f"{config.ANTHROPIC_BASE_URL.rstrip('/')}/v1/messages"
    r = httpx.post(url, json=payload, headers=headers, timeout=config.SYNTHESIS_TIMEOUT_SEC)
    r.raise_for_status()
    data = r.json()
    return "".join(b["text"] for b in data["content"] if b.get("type") == "text")


def _extract_cited_indices(body: str) -> list[int]:
    indices: list[int] = []
    seen: set[int] = set()
    for m in re.finditer(r"\[(\d+(?:\s*,\s*\d+)*)\]", body):
        for piece in m.group(1).split(","):
            try:
                n = int(piece.strip())
                if n not in seen:
                    seen.add(n); indices.append(n)
            except ValueError:
                continue
    return indices


def map_citations(body: str, chunks: list[Chunk]) -> list[Citation]:
    out: list[Citation] = []
    for idx in _extract_cited_indices(body):
        if 1 <= idx <= len(chunks):
            c = chunks[idx - 1]
            out.append(Citation(citation_index=idx, chunk_id=c.chunk_id,
                                document_id=c.document_id,
                                document_title=c.document_title))
    return out
```

- [ ] **Step 4: Update `synthesis/generate.py`** to use the shared helper. Replace its top imports and remove the now-moved `Citation`, `_extract_cited_indices`, and the inline httpx call. The new file:

```python
"""Topic → retrieve → draft. Separate from synthesize.py (per-document mode)."""
from __future__ import annotations

from retrieval.search import Chunk
from synthesis.llm import call_claude, map_citations, Citation  # Citation re-exported

DOC_TYPES = [
    "process", "architecture", "meeting_summary",
    "decision_record", "adr", "release_note",
]

DOC_TYPE_INSTRUCTIONS = {
    "process": "Write a step-by-step process document. Number the steps. Each step is one action.",
    "architecture": "Write an architecture overview. Cover components, how they connect, and data flow.",
    "meeting_summary": "Write a meeting summary. Sections: attendees (if known), decisions, action items, open questions.",
    "decision_record": "Write a decision record. Sections: context, decision, consequences.",
    "adr": "Write an Architecture Decision Record. Sections: status, context, decision, consequences, alternatives considered.",
    "release_note": "Write release notes. Sections: highlights, changes, breaking changes, upgrade notes.",
}

SYSTEM_PROMPT = """You write internal documentation for VOZIQ. You are given a topic and a set of source chunks.
Write a clear, accurate document using ONLY the information in the source chunks.
Cite sources inline using [N] markers matching the chunk numbers provided.
If the sources do not cover something needed for the topic, say "Not covered by available sources" rather than inventing detail."""


def _build_user_prompt(topic: str, doc_type: str, chunks: list[Chunk]) -> str:
    instruction = DOC_TYPE_INSTRUCTIONS.get(doc_type, DOC_TYPE_INSTRUCTIONS["process"])
    parts = [f"Topic: {topic}", f"Document type: {doc_type}",
             f"Instructions: {instruction}", "", "Source chunks:"]
    for i, c in enumerate(chunks, start=1):
        parts.append(f"[{i}] {c.document_title}\n{c.content}")
        parts.append("")
    parts.append("Write the document now. Use [N] inline citations matching chunks above.")
    return "\n".join(parts)


def generate(topic: str, doc_type: str, chunks: list[Chunk]) -> tuple[str, list[Citation]]:
    """Build prompt, call Claude, parse cited indices, return (body, citations)."""
    if doc_type not in DOC_TYPES:
        doc_type = "process"
    user_prompt = _build_user_prompt(topic, doc_type, chunks)
    body = call_claude(SYSTEM_PROMPT, user_prompt)
    return body, map_citations(body, chunks)
```

- [ ] **Step 5: Run both test modules, confirm PASS:**
`... python -m pytest tests/synthesis/test_llm.py tests/synthesis/test_generate.py -v`
Expected: all pass (the existing `test_generate.py` still works because `generate()` behavior and the re-exported `Citation` are unchanged).

- [ ] **Step 6: Commit:**
```bash
git add synthesis/llm.py synthesis/generate.py tests/synthesis/test_llm.py
git commit -m "refactor(synthesis): extract shared Claude call + citations to synthesis/llm.py"
```

---

## Task 2: Chat schema tables

**Files:**
- Modify: `db/schema.sql` (append after the DOCUMENTS → SPACES MIGRATION block, at end of file)
- Modify: `tests/conftest.py` (TRUNCATE list)

- [ ] **Step 1: Append to `db/schema.sql`:**

```sql
-- ─────────────────────────────────────────
-- CHAT
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS conversations (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title       TEXT NOT NULL DEFAULT 'New chat',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id);

CREATE TABLE IF NOT EXISTS messages (
    id               SERIAL PRIMARY KEY,
    conversation_id  INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role             TEXT NOT NULL CHECK (role IN ('user','assistant')),
    content          TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id);

CREATE TABLE IF NOT EXISTS message_citations (
    message_id      INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    chunk_id        INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    citation_index  INTEGER NOT NULL,
    PRIMARY KEY (message_id, chunk_id)
);
```

- [ ] **Step 2: Update the `db` fixture TRUNCATE in `tests/conftest.py`** to include the new tables (children first):
```python
        cur.execute("""
            TRUNCATE message_citations, messages, conversations,
                     draft_citations, drafts, review_queue, synthesis_runs,
                     knowledge_base, chunks, documents,
                     space_members, spaces, users RESTART IDENTITY CASCADE;
        """)
```

- [ ] **Step 3: Verify schema loads** by dropping + reloading the remote test schema then running a quick test:
```bash
source .venv/bin/activate
VB_DB_HOST=192.168.1.83 VB_DB_PORT=5434 VB_DB_NAME=openbrain_test VB_DB_USER=postgres VB_DB_PASSWORD=postgres python -c "import psycopg2; c=psycopg2.connect(host='192.168.1.83',port=5434,dbname='openbrain_test',user='postgres',password='postgres'); c.autocommit=True; c.cursor().execute('DROP SCHEMA public CASCADE; CREATE SCHEMA public;'); print('reset')"
VB_TEST_DB=openbrain_test VB_DB_HOST=192.168.1.83 VB_DB_PORT=5434 VB_DB_NAME=openbrain_test VB_DB_USER=postgres VB_DB_PASSWORD=postgres python -m pytest tests/accounts/test_core.py -q
```
Expected: accounts tests pass (proves schema.sql with the new tables loads and TRUNCATE works).

- [ ] **Step 4: Commit:**
```bash
git add db/schema.sql tests/conftest.py
git commit -m "feat(chat): conversations/messages/message_citations tables"
```

---

## Task 3: `chat/core.py` conversation store

**Files:**
- Create: `chat/__init__.py` (empty), `chat/core.py`
- Create: `tests/chat/__init__.py` (empty), `tests/chat/test_core.py`

- [ ] **Step 1: Write failing tests** `tests/chat/test_core.py`:

```python
import pytest
from accounts.core import get_or_create_user, ensure_common_space
from chat.core import (
    create_conversation, add_message, list_conversations,
    get_messages, conversation_owner,
)


def _seed_chunk(db, title, content, space_id):
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO documents (source_type, source_ref, title, content_hash,
                                       raw_content, status, file_extension, space_id, active)
               VALUES ('local_file', %s, %s, %s, '', 'indexed', '.md', %s, TRUE) RETURNING id""",
            (f"/fake/{title}", title, f"h-{title}", space_id),
        )
        doc_id = cur.fetchone()["id"]
        cur.execute(
            """INSERT INTO chunks (document_id, chunk_index, content, content_hash, token_count, embedding)
               VALUES (%s, 0, %s, %s, %s, %s::vector) RETURNING id""",
            (doc_id, content, f"ch-{title}", 3, str([0.1] * 768)),
        )
        chunk_id = cur.fetchone()["id"]
    db.commit()
    return chunk_id


def test_create_and_list_scoped_to_user(db):
    a = get_or_create_user(db, "alice"); b = get_or_create_user(db, "bob"); db.commit()
    ca = create_conversation(db, a["id"], title="Alice chat")
    create_conversation(db, b["id"], title="Bob chat")
    db.commit()
    a_list = list_conversations(db, a["id"])
    assert [c["id"] for c in a_list] == [ca]
    assert a_list[0]["title"] == "Alice chat"


def test_add_messages_and_get(db):
    a = get_or_create_user(db, "amy"); db.commit()
    common = ensure_common_space(db)
    chunk_id = _seed_chunk(db, "doc", "body", common)
    conv = create_conversation(db, a["id"]); db.commit()
    add_message(db, conv, "user", "what is churn?")
    cites = [{"citation_index": 1, "chunk_id": chunk_id}]
    add_message(db, conv, "assistant", "Churn is X [1]", citations=cites)
    db.commit()
    msgs = get_messages(db, conv)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"] == "Churn is X [1]"
    assert msgs[1]["citations"][0]["chunk_id"] == chunk_id


def test_conversation_owner(db):
    a = get_or_create_user(db, "ann"); db.commit()
    conv = create_conversation(db, a["id"]); db.commit()
    assert conversation_owner(db, conv) == a["id"]
    assert conversation_owner(db, 999999) is None
```

- [ ] **Step 2: Run, confirm FAIL** (`chat.core` missing).

- [ ] **Step 3: Create `chat/__init__.py` (empty) and `chat/core.py`:**

```python
"""Conversation + message store for the chat interface."""
from __future__ import annotations
from psycopg2.extras import RealDictCursor


def create_conversation(conn, user_id: int, title: str = "New chat") -> int:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "INSERT INTO conversations (user_id, title) VALUES (%s, %s) RETURNING id",
            (user_id, title),
        )
        return cur.fetchone()["id"]


def conversation_owner(conn, conversation_id: int) -> int | None:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT user_id FROM conversations WHERE id = %s", (conversation_id,))
        row = cur.fetchone()
        return row["user_id"] if row else None


def set_title(conn, conversation_id: int, title: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE conversations SET title = %s WHERE id = %s",
                    (title[:60], conversation_id))


def add_message(conn, conversation_id: int, role: str, content: str,
                citations: list[dict] | None = None) -> int:
    """Insert a message; for assistant messages, insert message_citations rows.
    Each citation dict: {"citation_index": int, "chunk_id": int}. Bumps updated_at."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (%s,%s,%s) RETURNING id",
            (conversation_id, role, content),
        )
        msg_id = cur.fetchone()["id"]
        for c in (citations or []):
            cur.execute(
                "INSERT INTO message_citations (message_id, chunk_id, citation_index) "
                "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                (msg_id, c["chunk_id"], c["citation_index"]),
            )
        cur.execute("UPDATE conversations SET updated_at = now() WHERE id = %s",
                    (conversation_id,))
        return msg_id


def list_conversations(conn, user_id: int) -> list[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id, title, updated_at FROM conversations "
            "WHERE user_id = %s ORDER BY updated_at DESC", (user_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def get_messages(conn, conversation_id: int) -> list[dict]:
    """Ordered messages; assistant messages include their citations with doc titles."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id, role, content, created_at FROM messages "
            "WHERE conversation_id = %s ORDER BY id", (conversation_id,),
        )
        msgs = [dict(r) for r in cur.fetchall()]
        for m in msgs:
            cur.execute(
                """SELECT mc.citation_index, mc.chunk_id, d.id AS document_id, d.title
                     FROM message_citations mc
                     JOIN chunks c ON c.id = mc.chunk_id
                     JOIN documents d ON d.id = c.document_id
                    WHERE mc.message_id = %s ORDER BY mc.citation_index""",
                (m["id"],),
            )
            m["citations"] = [dict(r) for r in cur.fetchall()]
        return msgs
```

- [ ] **Step 4: Run, confirm PASS:** `... python -m pytest tests/chat/test_core.py -v`

- [ ] **Step 5: Commit:**
```bash
git add chat/__init__.py chat/core.py tests/chat/__init__.py tests/chat/test_core.py
git commit -m "feat(chat): conversation/message store (chat/core.py)"
```

---

## Task 4: `chat/answer.py` RAG answerer

**Files:**
- Create: `chat/answer.py`
- Test: `tests/chat/test_answer.py`

- [ ] **Step 1: Write failing test** `tests/chat/test_answer.py`:

```python
import chat.answer as ca
from retrieval.search import Chunk


def _chunk(cid, doc_id, title, content):
    return Chunk(chunk_id=cid, document_id=doc_id, document_title=title,
                 source_type="local_file", file_extension=".md",
                 content=content, rrf_score=0.5)


def test_answer_returns_body_and_citations(monkeypatch):
    monkeypatch.setattr(ca, "call_claude", lambda system, user: "Churn means X [1]. More [2].")
    chunks = [_chunk(11, 1, "a.md", "churn def"), _chunk(22, 2, "b.md", "more")]
    body, citations = ca.answer("what is churn?", chunks, history=[])
    assert "[1]" in body
    assert {c.chunk_id for c in citations} == {11, 22}


def test_answer_includes_history_in_prompt(monkeypatch):
    captured = {}
    def fake(system, user):
        captured["user"] = user
        return "ok"
    monkeypatch.setattr(ca, "call_claude", fake)
    ca.answer("follow up", [_chunk(1, 1, "a", "x")],
              history=[{"role": "user", "content": "first q"},
                       {"role": "assistant", "content": "first a"}])
    assert "first q" in captured["user"] and "first a" in captured["user"]
```

- [ ] **Step 2: Run, confirm FAIL** (`chat.answer` missing).

- [ ] **Step 3: Create `chat/answer.py`:**

```python
"""RAG answerer for the chat interface: question + scoped chunks + history -> cited answer."""
from __future__ import annotations

from retrieval.search import Chunk
from synthesis.llm import call_claude, map_citations, Citation

SYSTEM_PROMPT = """You are an assistant answering questions about VOZIQ's internal knowledge base.
Answer using ONLY the information in the numbered source chunks provided.
Cite sources inline using [N] markers matching the chunk numbers.
If the sources do not contain the answer, say so plainly rather than inventing detail.
Be concise and conversational."""

_MAX_HISTORY_TURNS = 10


def _build_prompt(question: str, chunks: list[Chunk], history: list[dict]) -> str:
    parts: list[str] = []
    if history:
        parts.append("Conversation so far:")
        for h in history[-_MAX_HISTORY_TURNS:]:
            who = "User" if h["role"] == "user" else "Assistant"
            parts.append(f"{who}: {h['content']}")
        parts.append("")
    parts.append("Source chunks:")
    for i, c in enumerate(chunks, start=1):
        parts.append(f"[{i}] {c.document_title}\n{c.content}")
        parts.append("")
    parts.append(f"Question: {question}")
    parts.append("Answer now, using [N] inline citations matching the chunks above.")
    return "\n".join(parts)


def answer(question: str, chunks: list[Chunk],
           history: list[dict]) -> tuple[str, list[Citation]]:
    body = call_claude(SYSTEM_PROMPT, _build_prompt(question, chunks, history))
    return body, map_citations(body, chunks)
```

- [ ] **Step 4: Run, confirm PASS:** `... python -m pytest tests/chat/test_answer.py -v`

- [ ] **Step 5: Commit:**
```bash
git add chat/answer.py tests/chat/test_answer.py
git commit -m "feat(chat): RAG answerer (chat/answer.py)"
```

---

## Task 5: webui `/chat` routes + templates

**Files:**
- Create: `webui/routes/chat.py`, `webui/templates/chat.html`, `webui/templates/_chat_messages.html`
- Modify: `webui/app.py` (register router), `webui/templates/base.html` (nav link)
- Test: `tests/webui/test_chat.py`

- [ ] **Step 1: Write failing tests** `tests/webui/test_chat.py`:

```python
import pytest
import respx
import httpx
from httpx import AsyncClient, ASGITransport
from webui.app import create_app
from accounts.core import get_or_create_user, ensure_common_space, accessible_space_ids


@pytest.fixture
async def client():
    app = create_app(start_workers=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.cookies.set("ob_user", "chatuser")
        yield c


def _seed_chunk(db, title, content, space_id):
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO documents (source_type, source_ref, title, content_hash,
                                       raw_content, status, file_extension, space_id, active)
               VALUES ('local_file', %s, %s, %s, '', 'indexed', '.md', %s, TRUE) RETURNING id""",
            (f"/fake/{title}", title, f"h-{title}", space_id),
        )
        doc_id = cur.fetchone()["id"]
        cur.execute(
            """INSERT INTO chunks (document_id, chunk_index, content, content_hash, token_count, embedding)
               VALUES (%s, 0, %s, %s, %s, %s::vector)""",
            (doc_id, content, f"ch-{title}", 3, str([0.1] * 768)),
        )
    db.commit()


async def test_chat_requires_login(db):
    app = create_app(start_workers=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/chat", follow_redirects=False)
        assert r.status_code in (302, 307) and "/login" in r.headers["location"]


async def test_new_conversation_then_message(client, db, monkeypatch):
    # mock embedding (retrieval) and Claude
    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"embedding": [0.1] * 768}
    monkeypatch.setattr("ingestion.core.requests.post", lambda *a, **k: FakeResp())
    common = ensure_common_space(db)
    _seed_chunk(db, "churn", "churn is when customers leave", common)
    import chat.answer as ca
    monkeypatch.setattr(ca, "call_claude", lambda system, user: "Customers leaving [1].")

    r = await client.post("/chat/new", follow_redirects=False)
    assert r.status_code in (302, 303)
    conv_url = r.headers["location"]            # /chat/{id}
    conv_id = int(conv_url.rstrip("/").split("/")[-1])

    r = await client.post(f"/chat/{conv_id}/message", data={"question": "what is churn?"})
    assert r.status_code == 200
    assert "Customers leaving" in r.text         # assistant answer rendered

    with db.cursor() as cur:
        cur.execute("SELECT role, content FROM messages WHERE conversation_id=%s ORDER BY id", (conv_id,))
        rows = cur.fetchall()
    assert [x["role"] for x in rows] == ["user", "assistant"]


async def test_cannot_access_other_users_conversation(client, db):
    other = get_or_create_user(db, "intruder-victim")
    from chat.core import create_conversation
    conv = create_conversation(db, other["id"]); db.commit()
    r = await client.get(f"/chat/{conv}", follow_redirects=False)
    assert r.status_code == 404


async def test_conversation_list_scoped(client, db):
    me = get_or_create_user(db, "chatuser")
    other = get_or_create_user(db, "someone-else")
    from chat.core import create_conversation
    create_conversation(db, me["id"], title="MINE-XYZ")
    create_conversation(db, other["id"], title="THEIRS-XYZ")
    db.commit()
    r = await client.get("/chat")
    assert "MINE-XYZ" in r.text and "THEIRS-XYZ" not in r.text
```

- [ ] **Step 2: Run, confirm FAIL** (no `/chat` routes).

- [ ] **Step 3: Create `webui/routes/chat.py`:**

```python
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
        existing = list_conversations(conn, user["id"])
        # title from first question
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

    # render only the two newest turns (user + assistant)
    return render(request, "_chat_messages.html", {"messages": messages[-2:]})
```

- [ ] **Step 4: Create `webui/templates/_chat_messages.html`:**

```html
{% for m in messages %}
<div class="chat-msg {{ m.role }}">
  <div class="chat-role">{{ m.role }}</div>
  <div class="chat-content">{{ m.content }}</div>
  {% if m.citations %}
  <div class="chat-cites" style="color: var(--muted); font-size: 12px; margin-top: 4px;">
    {% for c in m.citations %}<span>[{{ c.citation_index }}] {{ c.title }}</span>{% if not loop.last %} · {% endif %}{% endfor %}
  </div>
  {% endif %}
</div>
{% endfor %}
```

- [ ] **Step 5: Create `webui/templates/chat.html`** (READ `base.html` first to confirm block names + classes):

```html
{% extends "base.html" %}
{% block title %}Chat — OpenBrain{% endblock %}
{% block content %}
<div class="toolbar">
  <h2>Chat</h2>
  <form method="post" action="/chat/new"><button class="btn primary" type="submit">+ New chat</button></form>
</div>
<div style="display:flex; gap:16px;">
  <div style="width:220px; flex:none;">
    {% for c in conversations %}
      <div><a href="/chat/{{ c.id }}" {% if c.id == active %}style="font-weight:600"{% endif %}>{{ c.title }}</a></div>
    {% endfor %}
  </div>
  <div style="flex:1;">
    {% if active %}
      <div id="messages">
        {% include "_chat_messages.html" %}
      </div>
      <form hx-post="/chat/{{ active }}/message" hx-target="#messages" hx-swap="beforeend"
            hx-on::after-request="this.reset()" class="filters" style="margin-top:12px;">
        <input class="input" type="text" name="question" placeholder="Ask the knowledge base..." autofocus required style="flex:1;">
        <button class="btn primary" type="submit">Send</button>
      </form>
    {% else %}
      <p style="color: var(--muted);">Start a new chat or pick one on the left.</p>
    {% endif %}
  </div>
</div>
{% endblock %}
```

- [ ] **Step 6: Register the router in `webui/app.py`** — add `chat` to the import + include line in `create_app`:
```python
    from webui.routes import auth, upload, inventory, docgen, drafts, spaces, chat
    ...
    app.include_router(chat.router)
```

- [ ] **Step 7: Add a "Chat" nav link in `webui/templates/base.html`** — READ the file, find the sidebar nav links, add `<a href="/chat" ...>Chat</a>` matching the existing pattern (including the `{% if page == 'chat' %}active{% endif %}` style if used).

- [ ] **Step 8: Run, confirm PASS:** `... python -m pytest tests/webui/test_chat.py -v`

- [ ] **Step 9: Commit:**
```bash
git add webui/routes/chat.py webui/templates/chat.html webui/templates/_chat_messages.html webui/app.py webui/templates/base.html tests/webui/test_chat.py
git commit -m "feat(webui): /chat RAG conversation interface"
```

---

## Task 6: Full verification + docs

- [ ] **Step 1: Drop + reload test schema, run the whole suite:**
```bash
source .venv/bin/activate
VB_DB_HOST=192.168.1.83 VB_DB_PORT=5434 VB_DB_NAME=openbrain_test VB_DB_USER=postgres VB_DB_PASSWORD=postgres python -c "import psycopg2; c=psycopg2.connect(host='192.168.1.83',port=5434,dbname='openbrain_test',user='postgres',password='postgres'); c.autocommit=True; c.cursor().execute('DROP SCHEMA public CASCADE; CREATE SCHEMA public;'); print('reset')"
VB_TEST_DB=openbrain_test VB_DB_HOST=192.168.1.83 VB_DB_PORT=5434 VB_DB_NAME=openbrain_test VB_DB_USER=postgres VB_DB_PASSWORD=postgres python -m pytest -q
```
Expected: all pass. Fix any straggler (e.g. an old test that imported `_extract_cited_indices`/`Citation` from `generate` — `Citation` is re-exported so that still works; if anything imported `_extract_cited_indices` from `generate`, update it to `synthesis.llm`).

- [ ] **Step 2: Add a "Chat" section to `README.md`** (after the Webui pages section): describe `/chat`, persisted per-user conversations, space-scoped retrieval, inline citations, non-streaming.

- [ ] **Step 3: Commit:**
```bash
git add -A
git commit -m "docs: chat interface (README); align any stragglers"
```

---

## Self-Review

**Spec coverage:**
- Shared `synthesis/llm.py` (`call_claude`, `map_citations`, `Citation`) + `generate.py` refactor → Task 1. ✅
- Chat tables + conftest → Task 2. ✅
- `chat/core.py` store (create/add/list/get/owner/set_title) → Task 3. ✅
- `chat/answer.py` RAG answerer with history cap → Task 4. ✅
- webui routes (home/view/new/message), templates, nav, ownership, scoped retrieval, Claude-failure path → Task 5. ✅
- Full verification + docs → Task 6. ✅
- Testing matrix (llm, core, answer, webui auth/scoping/ownership/failure) → covered per task. ✅

**Placeholder scan:** none — every code/test step has full code.

**Type/name consistency:** `call_claude(system, user_prompt) -> str`, `map_citations(body, chunks)`, `Citation(citation_index, chunk_id, document_id, document_title)`, `answer(question, chunks, history)`, `create_conversation/add_message/list_conversations/get_messages/conversation_owner/set_title` — used identically across Tasks 1–5. Message-citation dict shape `{"citation_index","chunk_id"}` consistent between `add_message` (Task 3), `answer` mapping, and the route (Task 5). `messages[-2:]` partial render matches `_chat_messages.html` loop.

**Risk:** `chat_message` reads `prior` (history) BEFORE inserting the user message, so history excludes the current question — correct. The `existing` variable in Task 5 Step 3 is unused; drop it during implementation.
