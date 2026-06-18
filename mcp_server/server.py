"""OpenBrain MCP server.

Exposes the RAG knowledge base over the Model Context Protocol via
Streamable HTTP transport on port 8001 (multi-client, long-lived).

Tools:
    search(query, source_types?, file_extensions?, namespaces?, top_k?) — hybrid retrieval
    fetch_document(document_id) — full content + metadata
    list_documents(status?, source?, namespace?, limit?) — inventory listing

Run:
    uvicorn mcp_server.server:app --host 0.0.0.0 --port 8001
"""
from __future__ import annotations
import logging
from datetime import datetime

from mcp.server.fastmcp import FastMCP

from accounts.core import user_by_token, accessible_space_ids
from db.connection import get_conn
from retrieval.search import SearchFilters, search as do_search


def _bearer_token() -> str | None:
    """Read the Authorization: Bearer token from the current MCP HTTP request.

    The streamable-HTTP transport populates RequestContext.request with the
    raw Starlette Request object.  We reach it via the request_ctx ContextVar
    exposed by mcp.server.lowlevel.server.  Outside a live request (e.g. in
    unit tests) the ContextVar is unset, so we return None gracefully.
    """
    try:
        from mcp.server.lowlevel.server import request_ctx
        ctx = request_ctx.get()          # raises LookupError outside a request
        req = ctx.request                # Starlette Request (or None for SSE)
        if req is None:
            return None
        auth = req.headers.get("authorization", "")
    except Exception:
        return None
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def _resolve_scope(token: str | None) -> tuple[dict | None, list[int]]:
    """Resolve a bearer token to (user, accessible_space_ids).

    Returns (None, []) when the token is absent or not found.
    """
    if not token:
        return None, []
    with get_conn() as conn:
        user = user_by_token(conn, token)
        if not user:
            return None, []
        return user, accessible_space_ids(conn, user["id"])

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("openbrain.mcp")

mcp = FastMCP(
    "openbrain",
    instructions=(
        "OpenBrain RAG knowledge base. Use `search` for semantic + keyword "
        "retrieval over indexed company documents. Use `fetch_document` to "
        "get the full content of a document by id. Use `list_documents` to "
        "browse what is indexed."
    ),
)


def _serialize_ts(value) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


@mcp.tool()
def search(
    query: str,
    source_types: list[str] | None = None,
    file_extensions: list[str] | None = None,
    namespaces: list[str] | None = None,
    top_k: int = 12,
) -> list[dict]:
    """Hybrid pgvector ANN + tsvector BM25 retrieval, RRF-merged.

    Args:
        query: Natural-language query.
        source_types: Filter to ['local_file','web_ui','outlook_email']. None = all.
        file_extensions: Filter to ['.pdf','.md','.docx', ...]. None = all.
        namespaces: Filter to one or more namespaces (e.g. ['code','operations']). None = all.
        top_k: Number of chunks to return (default 12).

    Returns: list of {chunk_id, document_id, document_title, source_type,
                      file_extension, content, rrf_score}.
    """
    filters = SearchFilters(
        source_types=source_types or [],
        file_extensions=file_extensions or [],
        namespaces=namespaces or [],
    )
    results = do_search(query, filters=filters, top_k=top_k)
    return [
        {
            "chunk_id": r.chunk_id,
            "document_id": r.document_id,
            "document_title": r.document_title,
            "source_type": r.source_type,
            "file_extension": r.file_extension,
            "content": r.content,
            "rrf_score": r.rrf_score,
        }
        for r in results
    ]


@mcp.tool()
def fetch_document(document_id: int) -> dict:
    """Return full content and metadata for one document.

    Args:
        document_id: Integer id from search results or list_documents.

    Returns: {id, title, source_type, source_ref, ingested_at, status,
              file_extension, file_size, chunk_count, raw_content}
             or {"error": "..."} if not found.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.id, d.title, d.source_type, d.source_ref, d.ingested_at,
                       d.status, d.file_extension, d.file_size, d.namespace, d.raw_content,
                       (SELECT COUNT(*) FROM chunks c WHERE c.document_id = d.id) AS chunk_count
                  FROM documents d
                 WHERE d.id = %s AND d.active = TRUE
                """,
                (document_id,),
            )
            row = cur.fetchone()
            if not row:
                return {"error": f"document {document_id} not found or inactive"}
            cols = [c.name for c in cur.description]
            doc = dict(zip(cols, row))
            doc["ingested_at"] = _serialize_ts(doc["ingested_at"])
            return doc


@mcp.tool()
def list_documents(
    status: str | None = None,
    source: str | None = None,
    namespace: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """List indexed documents (newest first).

    Args:
        status: Filter by status ('indexed','queued','processing','failed'). None = all.
        source: Filter by source_type ('local_file','web_ui','outlook_email'). None = all.
        namespace: Filter by namespace (e.g. 'code','operations'). None = all.
        limit: Max rows (default 50).

    Returns: list of {id, title, source_type, file_extension, status,
                      file_size, namespace, chunk_count, ingested_at}.
    """
    where: list[str] = ["active = TRUE"]
    args: list = []
    if status:
        where.append("status = %s")
        args.append(status)
    if source:
        where.append("source_type = %s")
        args.append(source)
    if namespace:
        where.append("namespace = %s")
        args.append(namespace)
    sql = f"""
        SELECT d.id, d.title, d.source_type, d.file_extension, d.status,
               d.file_size, d.namespace, d.ingested_at,
               (SELECT COUNT(*) FROM chunks c WHERE c.document_id = d.id) AS chunk_count
          FROM documents d
         WHERE {' AND '.join(where)}
         ORDER BY d.ingested_at DESC NULLS LAST
         LIMIT %s
    """
    args.append(limit)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            cols = [c.name for c in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            for r in rows:
                r["ingested_at"] = _serialize_ts(r["ingested_at"])
            return rows


# ASGI app for uvicorn — Streamable HTTP transport at POST /mcp
app = mcp.streamable_http_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
