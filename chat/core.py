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
    """Insert a message; for assistant messages insert message_citations rows.
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
