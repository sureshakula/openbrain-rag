"""Shared Claude call + citation mapping for synthesis and chat."""
from __future__ import annotations
import re
from dataclasses import dataclass

import httpx

import config  # read settings dynamically at call time
from retrieval.search import Chunk


@dataclass
class Citation:
    citation_index: int
    chunk_id: int
    document_id: int
    document_title: str


def call_claude(system: str, user_prompt: str) -> str:
    """POST to the Anthropic-compatible endpoint; return concatenated assistant text."""
    payload = {
        "model": config.SYNTHESIS_MODEL,
        "max_tokens": config.SYNTHESIS_MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    headers = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
    if config.ANTHROPIC_AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {config.ANTHROPIC_AUTH_TOKEN}"
    if config.ANTHROPIC_API_KEY:
        headers["x-api-key"] = config.ANTHROPIC_API_KEY
    url = f"{config.ANTHROPIC_BASE_URL.rstrip('/')}/v1/messages"
    r = httpx.post(url, json=payload, headers=headers, timeout=config.SYNTHESIS_TIMEOUT_SEC)
    r.raise_for_status()
    data = r.json()
    return "".join(b["text"] for b in data["content"] if b.get("type") == "text")


def _extract_cited_indices(body: str) -> list[int]:
    indices: list[int] = []
    seen: set[int] = set()
    for m in re.finditer(r"\[(\d+(?:\s*,\s*\d+)*)\]", body):
        for piece in m.group(1).split(","):
            try:
                n = int(piece.strip())
                if n not in seen:
                    seen.add(n); indices.append(n)
            except ValueError:
                continue
    return indices


def map_citations(body: str, chunks: list[Chunk]) -> list[Citation]:
    out: list[Citation] = []
    for idx in _extract_cited_indices(body):
        if 1 <= idx <= len(chunks):
            c = chunks[idx - 1]
            out.append(Citation(citation_index=idx, chunk_id=c.chunk_id,
                                document_id=c.document_id,
                                document_title=c.document_title))
    return out
