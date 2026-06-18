"""Tests for ingestion/ingest_local.py — CLI space/user routing."""
from pathlib import Path
from accounts.core import ensure_common_space, get_or_create_user, space_by_name


def test_ingest_file_routes_to_space(db, monkeypatch, tmp_path):
    import ingestion.ingest_local as il
    monkeypatch.setattr(il, "embed", lambda *a, **k: [0.1] * 768)
    common = ensure_common_space(db)
    db.commit()
    p = tmp_path / "note.md"
    p.write_text("hello world")
    il.ingest_file(db, p, dry_run=False, space_id=common, created_by=None)
    db.commit()
    with db.cursor() as cur:
        cur.execute("SELECT space_id FROM documents WHERE title='note'")
        assert cur.fetchone()["space_id"] == common
