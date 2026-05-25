import pytest
from pathlib import Path
from webui.workers.file_dispatch import extract_text, UnsupportedFileType, NotYetImplemented


def test_extract_txt(tmp_path: Path):
    p = tmp_path / "hello.txt"
    p.write_text("hello world")
    text, meta = extract_text(p)
    assert "hello world" in text
    assert meta["extractor"] == "text"

def test_extract_md(tmp_path: Path):
    p = tmp_path / "doc.md"
    p.write_text("# Title\n\nBody")
    text, _ = extract_text(p)
    assert "Title" in text and "Body" in text

def test_extract_pdf(tmp_path: Path):
    import fitz  # pymupdf
    p = tmp_path / "doc.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "PDF hello")
    doc.save(p)
    doc.close()
    text, meta = extract_text(p)
    assert "PDF hello" in text
    assert meta["extractor"] == "pdf"

def test_extract_unsupported(tmp_path: Path):
    p = tmp_path / "file.xyz"
    p.write_bytes(b"binary junk")
    with pytest.raises(UnsupportedFileType):
        extract_text(p)

def test_extract_mp4_stub(tmp_path: Path):
    p = tmp_path / "recording.mp4"
    p.write_bytes(b"fake mp4")
    with pytest.raises(NotYetImplemented):
        extract_text(p)
