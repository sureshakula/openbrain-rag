"""Topic → retrieve → draft. Separate from synthesize.py (per-document mode)."""
from __future__ import annotations
import re
from dataclasses import dataclass

import httpx

from config import ANTHROPIC_API_KEY, SYNTHESIS_MODEL, SYNTHESIS_MAX_TOKENS
from retrieval.search import Chunk


DOC_TYPES = [
    "process", "architecture", "meeting_summary",
    "decision_record", "adr", "release_note",
]

DOC_TYPE_INSTRUCTIONS = {
    "process": "Write a step-by-step process document. Number the steps. Each step is one action.",
    "architecture": "Write an architecture overview. Cover components, how they connect, and data flow.",
    "meeting_summary": "Write a meeting summary. Sections: attendees (if known), decisions, action items, open questions.",
    "decision_record": "Write a decision record. Sections: context, decision, consequences.",
    "adr": "Write an Architecture Decision Record. Sections: status, context, decision, consequences, alternatives considered.",
    "release_note": "Write release notes. Sections: highlights, changes, breaking changes, upgrade notes.",
}


@dataclass
class Citation:
    citation_index: int
    chunk_id: int
    document_id: int
    document_title: str


SYSTEM_PROMPT = """You write internal documentation for VOZIQ. You are given a topic and a set of source chunks.
Write a clear, accurate document using ONLY the information in the source chunks.
Cite sources inline using [N] markers matching the chunk numbers provided.
If the sources do not cover something needed for the topic, say "Not covered by available sources" rather than inventing detail."""


def _build_user_prompt(topic: str, doc_type: str, chunks: list[Chunk]) -> str:
    instruction = DOC_TYPE_INSTRUCTIONS.get(doc_type, DOC_TYPE_INSTRUCTIONS["process"])
    parts = [
        f"Topic: {topic}",
        f"Document type: {doc_type}",
        f"Instructions: {instruction}",
        "",
        "Source chunks:",
    ]
    for i, c in enumerate(chunks, start=1):
        parts.append(f"[{i}] {c.document_title}\n{c.content}")
        parts.append("")
    parts.append("Write the document now. Use [N] inline citations matching chunks above.")
    return "\n".join(parts)


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


def generate(topic: str, doc_type: str, chunks: list[Chunk]) -> tuple[str, list[Citation]]:
    """Build prompt, call Claude, parse cited indices, return (body, citations)."""
    if doc_type not in DOC_TYPES:
        doc_type = "process"
    user_prompt = _build_user_prompt(topic, doc_type, chunks)
    payload = {
        "model": SYNTHESIS_MODEL,
        "max_tokens": SYNTHESIS_MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    r = httpx.post("https://api.anthropic.com/v1/messages",
                   json=payload, headers=headers, timeout=120)
    r.raise_for_status()
    data = r.json()
    body = "".join(block["text"] for block in data["content"] if block.get("type") == "text")
    cited_indices = _extract_cited_indices(body)
    citations: list[Citation] = []
    for idx in cited_indices:
        if 1 <= idx <= len(chunks):
            c = chunks[idx - 1]
            citations.append(Citation(citation_index=idx, chunk_id=c.chunk_id,
                                       document_id=c.document_id,
                                       document_title=c.document_title))
    return body, citations
