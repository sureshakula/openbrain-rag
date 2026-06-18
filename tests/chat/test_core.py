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
