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
    # Neutralize Anthropic settings so synthesis tests mock the default endpoint
    # regardless of a real ANTHROPIC_BASE_URL / token in the runtime .env.
    os.environ["ANTHROPIC_BASE_URL"] = "https://api.anthropic.com"
    os.environ["ANTHROPIC_AUTH_TOKEN"] = ""
    os.environ["ANTHROPIC_API_KEY"] = "test"
    # Reload config so module-level constants pick up the env
    import importlib, config
    importlib.reload(config)
    import db.connection
    importlib.reload(db.connection)
    yield


@pytest.fixture(autouse=True, scope="session")
def _ensure_test_schema(_force_test_db_env, db_dsn):
    """Apply db/schema.sql to the test DB so tables exist no matter how the
    database was provisioned (fresh volume, host postgres, etc). schema.sql is
    idempotent (CREATE ... IF NOT EXISTS / ADD COLUMN IF NOT EXISTS)."""
    schema_path = Path(__file__).resolve().parent.parent / "db" / "schema.sql"
    conn = psycopg2.connect(db_dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(schema_path.read_text())
    conn.close()
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
            TRUNCATE settings, message_citations, messages, conversations,
                     draft_citations, drafts, review_queue, synthesis_runs,
                     knowledge_base, chunks, documents,
                     space_members, spaces, users RESTART IDENTITY CASCADE;
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
