"""Hybrid retrieval: pgvector cosine ANN + Postgres tsvector BM25, RRF-merged."""
from __future__ import annotations
from dataclasses import dataclass, field

from config import RETRIEVAL_TOP_K, RETRIEVAL_RRF_K
from db.connection import get_conn
from ingestion.core import embed


@dataclass
class SearchFilters:
    source_types: list[str] = field(default_factory=list)
    file_extensions: list[str] = field(default_factory=list)
    space_ids: list[int] | None = None   # None = no scope filter; [] = fail-closed (match nothing)


@dataclass
class Chunk:
    chunk_id: int
    document_id: int
    document_title: str
    source_type: str
    file_extension: str | None
    content: str
    rrf_score: float


def _build_where(filters: SearchFilters | None) -> tuple[str, list]:
    clauses = ["d.active = TRUE"]
    args: list = []
    if filters and filters.source_types:
        clauses.append("d.source_type = ANY(%s)"); args.append(filters.source_types)
    if filters and filters.file_extensions:
        clauses.append("d.file_extension = ANY(%s)"); args.append(filters.file_extensions)
    if filters is not None and filters.space_ids is not None:
        clauses.append("d.space_id = ANY(%s)"); args.append(filters.space_ids)
    return " AND ".join(clauses), args


def search(query: str, filters: SearchFilters | None = None,
           top_k: int = RETRIEVAL_TOP_K) -> list[Chunk]:
    """Hybrid pgvector + BM25, RRF-merged. Returns Chunk objects with rrf_score."""
    query_emb = embed(query)
    if query_emb is None:
        return _bm25_only(query, filters, top_k)

    where, args = _build_where(filters)
    over_fetch = top_k * 4
    emb_str = str(query_emb)

    vec_sql = f"""
        SELECT c.id AS chunk_id, c.document_id, d.title, d.source_type, d.file_extension,
               c.content, c.embedding <=> %s::vector AS distance
          FROM chunks c JOIN documents d ON d.id = c.document_id
         WHERE {where} AND c.embedding IS NOT NULL
         ORDER BY c.embedding <=> %s::vector
         LIMIT {over_fetch}
    """
    bm_sql = f"""
        SELECT c.id AS chunk_id, c.document_id, d.title, d.source_type, d.file_extension,
               c.content, ts_rank(c.content_tsv, plainto_tsquery('english', %s)) AS rank
          FROM chunks c JOIN documents d ON d.id = c.document_id
         WHERE {where} AND c.content_tsv @@ plainto_tsquery('english', %s)
         ORDER BY rank DESC
         LIMIT {over_fetch}
    """

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(vec_sql, [emb_str, *args, emb_str])
            vec_rows = cur.fetchall()
            cur.execute(bm_sql, [query, *args, query])
            bm_rows = cur.fetchall()

    return _rrf_merge(vec_rows, bm_rows, top_k)


def _bm25_only(query: str, filters: SearchFilters | None, top_k: int) -> list[Chunk]:
    where, args = _build_where(filters)
    sql = f"""
        SELECT c.id, c.document_id, d.title, d.source_type, d.file_extension,
               c.content, ts_rank(c.content_tsv, plainto_tsquery('english', %s)) AS rank
          FROM chunks c JOIN documents d ON d.id = c.document_id
         WHERE {where} AND c.content_tsv @@ plainto_tsquery('english', %s)
         ORDER BY rank DESC
         LIMIT {top_k}
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, [query, *args, query])
            return [Chunk(*row[:6], rrf_score=float(row[6])) for row in cur.fetchall()]


def _rrf_merge(vec_rows, bm_rows, top_k: int) -> list[Chunk]:
    scores: dict[int, float] = {}
    payloads: dict[int, tuple] = {}
    for rank, row in enumerate(vec_rows, start=1):
        cid = row[0]
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (RETRIEVAL_RRF_K + rank)
        payloads[cid] = row
    for rank, row in enumerate(bm_rows, start=1):
        cid = row[0]
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (RETRIEVAL_RRF_K + rank)
        payloads.setdefault(cid, row)
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
    out: list[Chunk] = []
    for cid, score in ordered:
        row = payloads[cid]
        out.append(Chunk(
            chunk_id=row[0], document_id=row[1], document_title=row[2],
            source_type=row[3], file_extension=row[4], content=row[5],
            rrf_score=score,
        ))
    return out
