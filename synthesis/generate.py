"""Topic → retrieve → draft. Separate from synthesize.py (per-document mode)."""
from __future__ import annotations

from retrieval.search import Chunk
from synthesis.llm import call_claude, map_citations, Citation  # Citation re-exported

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

SYSTEM_PROMPT = """You write internal documentation for VOZIQ. You are given a topic and a set of source chunks.
Write a clear, accurate document using ONLY the information in the source chunks.
Cite sources inline using [N] markers matching the chunk numbers provided.
If the sources do not cover something needed for the topic, say "Not covered by available sources" rather than inventing detail."""


def _build_user_prompt(topic: str, doc_type: str, chunks: list[Chunk]) -> str:
    instruction = DOC_TYPE_INSTRUCTIONS.get(doc_type, DOC_TYPE_INSTRUCTIONS["process"])
    parts = [f"Topic: {topic}", f"Document type: {doc_type}",
             f"Instructions: {instruction}", "", "Source chunks:"]
    for i, c in enumerate(chunks, start=1):
        parts.append(f"[{i}] {c.document_title}\n{c.content}")
        parts.append("")
    parts.append("Write the document now. Use [N] inline citations matching chunks above.")
    return "\n".join(parts)


def generate(topic: str, doc_type: str, chunks: list[Chunk]) -> tuple[str, list[Citation]]:
    """Build prompt, call Claude, parse cited indices, return (body, citations)."""
    if doc_type not in DOC_TYPES:
        doc_type = "process"
    user_prompt = _build_user_prompt(topic, doc_type, chunks)
    body = call_claude(SYSTEM_PROMPT, user_prompt)
    return body, map_citations(body, chunks)
