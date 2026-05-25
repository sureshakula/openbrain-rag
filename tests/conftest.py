"""Shared pytest fixtures."""
import os
import pytest
import psycopg2
from psycopg2.extras import RealDictCursor
from pathlib import Path

TEST_DB = os.getenv("VB_TEST_DB", "openbrain_test")


@pytest.fixture(autouse=True, scope="session")
def _force_test_db_env():
    """Force the app's get_conn() to also point at the test DB during tests."""
    os.environ["VB_DB_HOST"] = os.getenv("VB_DB_HOST", "localhost")
    os.environ["VB_DB_PORT"] = os.getenv("VB_DB_PORT", "5432")
    os.environ["VB_DB_USER"] = os.getenv("VB_DB_USER", "postgres")
    os.environ["VB_DB_PASSWORD"] = os.getenv("VB_DB_PASSWORD", "postgres")
    os.environ["VB_DB_NAME"] = os.getenv("VB_DB_NAME", "openbrain_test")
    # Reload config so module-level constants pick up the env
    import importlib, config
    importlib.reload(config)
    import db.connection
    importlib.reload(db.connection)
    yield


@pytest.fixture(scope="session")
def db_dsn() -> str:
    return f"dbname={TEST_DB} user={os.getenv('VB_DB_USER','postgres')} password={os.getenv('VB_DB_PASSWORD','postgres')} host={os.getenv('VB_DB_HOST','localhost')} port={os.getenv('VB_DB_PORT','5432')}"


@pytest.fixture
def db(db_dsn):
    """Function-scoped raw connection. Truncates all webui tables before yielding."""
    conn = psycopg2.connect(db_dsn, cursor_factory=RealDictCursor)
    with conn.cursor() as cur:
        cur.execute("""
            TRUNCATE draft_citations, drafts, review_queue, synthesis_runs,
                     knowledge_base, chunks, documents RESTART IDENTITY CASCADE;
        """)
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def tmp_upload_dir(tmp_path, monkeypatch) -> Path:
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    monkeypatch.setenv("VB_UPLOAD_DIR", str(upload_dir))
    return upload_dir
