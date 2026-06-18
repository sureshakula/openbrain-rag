"""RAG answerer for the chat interface: question + scoped chunks + history -> cited answer."""
from __future__ import annotations

from retrieval.search import Chunk
from synthesis.llm import call_claude, map_citations, Citation

SYSTEM_PROMPT = """You are an assistant answering questions about VOZIQ's internal knowledge base.
Answer using ONLY the information in the numbered source chunks provided.
Cite sources inline using [N] markers matching the chunk numbers.
If the sources do not contain the answer, say so plainly rather than inventing detail.
Be concise and conversational."""

_MAX_HISTORY_TURNS = 10


def _build_prompt(question: str, chunks: list[Chunk], history: list[dict]) -> str:
    parts: list[str] = []
    if history:
        parts.append("Conversation so far:")
        for h in history[-_MAX_HISTORY_TURNS:]:
            who = "User" if h["role"] == "user" else "Assistant"
            parts.append(f"{who}: {h['content']}")
        parts.append("")
    parts.append("Source chunks:")
    for i, c in enumerate(chunks, start=1):
        parts.append(f"[{i}] {c.document_title}\n{c.content}")
        parts.append("")
    parts.append(f"Question: {question}")
    parts.append("Answer now, using [N] inline citations matching the chunks above.")
    return "\n".join(parts)


def answer(question: str, chunks: list[Chunk],
           history: list[dict]) -> tuple[str, list[Citation]]:
    body = call_claude(SYSTEM_PROMPT, _build_prompt(question, chunks, history))
    return body, map_citations(body, chunks)
