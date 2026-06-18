# Chat Interface — Design

**Date:** 2026-06-18
**Status:** Approved (pending spec review)
**Branch:** feat/webui

## Summary

A `/chat` RAG interface to converse with the document store. Each user turn retrieves space-scoped chunks for the question, Claude answers with inline `[n]` citations grounded in those chunks, and the turn is persisted. Conversations are owned per-user; retrieval is scoped to the user's accessible spaces (built on the multi-user spaces feature). Responses are non-streaming. Includes a small DRY refactor extracting the shared Claude-call + citation logic out of `synthesis/generate.py` into `synthesis/llm.py` so chat and doc-gen share it.

## Decisions (from brainstorming)

1. **Persisted conversations** — saved, revisitable, scoped per user.
2. **Full (non-streaming) responses** — reuse the existing synchronous Claude call.
3. **Answer + inline citations** — reuse the doc-gen `[n]`→chunk citation pattern.

## Goals

- Logged-in user asks questions at `/chat` and gets cited answers grounded in documents they can access.
- Multi-turn: prior messages in a conversation are included as context.
- Conversations persist and are listed; a user sees and continues only their own.
- Retrieval is scoped to the user's accessible spaces (no cross-space leakage).

## Non-Goals

- Streaming token output (future enhancement).
- Editing/branching messages, regenerate, or conversation sharing.
- Cross-conversation memory or retrieval over chat history itself.
- Changing retrieval ranking; it reuses `retrieval.search` as-is.

## Data Model (new tables in `db/schema.sql`, idempotent)

```sql
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

This block is appended after the MULTI-USER SPACES section in `schema.sql` (it references `users` and `chunks`). The per-test TRUNCATE list in `tests/conftest.py` gains `message_citations, messages, conversations`.

## Shared LLM helper (`synthesis/llm.py`) — the refactor

Extract from `generate.py`:
- `Citation` dataclass `(citation_index, chunk_id, document_id, document_title)`.
- `call_claude(system: str, user_prompt: str) -> str` — the httpx POST to `{ANTHROPIC_BASE_URL}/v1/messages` (reads `config.ANTHROPIC_*` / `config.SYNTHESIS_*` dynamically), returns the concatenated assistant text. Raises on HTTP error.
- `map_citations(body: str, chunks: list[Chunk]) -> list[Citation]` — `_extract_cited_indices` + map `[n]` → `chunks[n-1]`.

`generate.py` is updated to import these from `synthesis.llm` and keep only `DOC_TYPES`, `DOC_TYPE_INSTRUCTIONS`, `SYSTEM_PROMPT`, `_build_user_prompt`, and `generate()` (which now calls `call_claude` + `map_citations`). `Citation` is re-exported from `generate` for backward compatibility (existing imports `from synthesis.generate import ... Citation` keep working; `docgen.py`/tests import `generate, DOC_TYPES`).

## Chat answer (`chat/answer.py`)

```python
def answer(question: str, chunks: list[Chunk],
           history: list[dict]) -> tuple[str, list[Citation]]
```
- `history` is a list of `{"role","content"}` prior turns (user/assistant), oldest first.
- Builds a chat system prompt (conversational, grounded, cite `[n]`, say so when sources don't cover it) and a user prompt embedding the numbered source chunks + the question. Prior history is passed as prior `messages` to `call_claude` — extend `call_claude` to accept an optional `messages` list, OR build a single user prompt that includes a brief transcript. **Decision:** keep `call_claude(system, user_prompt)` simple; `answer()` includes a compact transcript of prior turns in the user prompt (YAGNI; avoids changing the doc-gen call signature). Retrieval uses the latest question only.
- Returns `(body, citations)` via `map_citations`.

## Conversation store (`chat/core.py`)

- `create_conversation(conn, user_id, title='New chat') -> int`
- `add_message(conn, conversation_id, role, content, citations=None) -> int` — inserts the message; for assistant messages, inserts `message_citations` rows; bumps `conversations.updated_at`.
- `list_conversations(conn, user_id) -> list[dict]` — id, title, updated_at; newest first; only this user's.
- `get_messages(conn, conversation_id) -> list[dict]` — ordered; each `{id, role, content, created_at, citations:[...]}` (assistant messages include their citations joined to document titles).
- `conversation_owner(conn, conversation_id) -> int | None` — for ownership checks.

## webui (`webui/routes/chat.py`)

All routes resolve `current_user`; redirect to `/login` if absent. Ownership enforced (a conversation not owned by the user → 404).

- `GET /chat` — render `chat.html`: conversation list (sidebar) + empty/new state.
- `GET /chat/{id}` — render `chat.html` with that conversation's messages (after ownership check).
- `POST /chat/new` — create a conversation, redirect to `/chat/{id}`.
- `POST /chat/{id}/message` (form field `question`) — ownership check; if the conversation has no title yet (still 'New chat'), set title from the first question (truncated ~60 chars); retrieve scoped chunks (`SearchFilters(space_ids=accessible_space_ids(user))`); call `answer(question, chunks, history)`; persist user + assistant messages (+citations); return the `_chat_messages.html` partial for the appended turn(s). On Claude error, persist the user message and return an assistant bubble with an error notice (no crash).

Templates: `chat.html` (full page: conversation list + message area + question form posting via HTMX to `/chat/{id}/message`, target appends to the message list) and `_chat_messages.html` (renders a list of messages with citations). Add a "Chat" sidebar nav link in `base.html`.

## Error Handling

- No cookie → redirect `/login`.
- `GET/POST /chat/{id}*` where the conversation isn't owned by the user → 404 (indistinguishable from missing).
- Claude/HTTP failure in `answer()` → the route catches it, still saves the user message, and renders an assistant error bubble ("Sorry — generation failed, try again."). No 500.
- Empty retrieval (no accessible chunks match) → `answer()` still runs; the grounded prompt yields a "not covered by available sources" style reply; zero citations saved.

## Testing

- **`synthesis/llm.py`**: `map_citations` maps `[1][2]` to the right chunks and ignores out-of-range; `call_claude` posts to the mocked endpoint and returns text (respx, default `api.anthropic.com`). Existing `test_generate.py` still passes against the refactored `generate.py`.
- **`chat/core.py`**: create/list scoped to user (other users' conversations not listed); add_message persists role+content; assistant citations stored; `get_messages` returns ordered messages with citations; `conversation_owner` correct.
- **`chat/answer.py`**: with mocked `call_claude`, returns body + citations mapped from numbered chunks.
- **webui `chat.py`**: `/chat` redirects without cookie; `POST /chat/{id}/message` persists a user + assistant message and returns the answer text; conversation list scoped to the current user; accessing another user's conversation → 404; retrieval is space-scoped (a question does not surface another user's private-space chunks as citations). Claude-failure path renders an error bubble, still 200.

## Implementation Order (for the plan)

1. `synthesis/llm.py` refactor + update `generate.py`; keep `test_generate.py` green.
2. Schema tables + conftest TRUNCATE.
3. `chat/core.py` store with tests.
4. `chat/answer.py` with tests.
5. webui `chat.py` + templates + nav, with tests.
6. Full suite + live smoke + docs (README chat section).

## Risks

- `call_claude` signature must stay compatible so doc-gen keeps working; the refactor keeps `(system, user_prompt) -> str`.
- History-in-user-prompt may get long for long conversations; acceptable for v1 (cap to the last ~10 turns in `answer()`).
