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
