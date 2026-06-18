from ingestion.core import chunk_text, sha256_text


def test_chunk_text_splits_on_paragraphs():
    text = "Para one.\n\nPara two.\n\nPara three."
    chunks = chunk_text(text, chunk_size=10, overlap=0)
    assert len(chunks) >= 1
    assert all(c.strip() for c in chunks)


def test_chunk_text_handles_oversize_paragraph():
    big = "x" * 5000
    chunks = chunk_text(big, chunk_size=100, overlap=10)
    assert len(chunks) > 1


def test_sha256_text_stable():
    assert sha256_text("hello") == sha256_text("hello")
    assert sha256_text("hello") != sha256_text("world")


def test_insert_document_uses_space_and_creator(db):
    from accounts.core import get_or_create_user, ensure_common_space
    from ingestion.core import insert_document
    u = get_or_create_user(db, "carol")
    common = ensure_common_space(db)
    doc_id = insert_document(
        db, source_type="local_file", source_ref="/x.md", title="x",
        raw_content="body", content_hash="h-x", file_size=4, file_extension=".md",
        status="indexed", space_id=common, created_by=u["id"],
    )
    with db.cursor() as cur:
        cur.execute("SELECT space_id, created_by FROM documents WHERE id=%s", (doc_id,))
        row = cur.fetchone()
    assert row["space_id"] == common and row["created_by"] == u["id"]


def test_documents_has_no_namespace_column(db):
    with db.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='documents' AND column_name='namespace'"
        )
        assert cur.fetchone() is None
