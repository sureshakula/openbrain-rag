import respx
import httpx
import pytest

from synthesis.generate import generate, DOC_TYPES
from retrieval.search import Chunk


def _chunk(cid: int, doc_id: int, title: str, content: str) -> Chunk:
    return Chunk(chunk_id=cid, document_id=doc_id, document_title=title,
                 source_type="local_file", file_extension=".md",
                 content=content, rrf_score=0.5)


@respx.mock
def test_generate_returns_body_and_citations():
    from config import SYNTHESIS_MODEL
    body_text = "Churn pipeline ingests transcripts [1] and produces scores [2]."
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(200, json={
            "content": [{"type": "text", "text": body_text}],
            "model": SYNTHESIS_MODEL, "usage": {"input_tokens": 10, "output_tokens": 5},
        })
    )
    chunks = [
        _chunk(101, 1, "churn-spec.md", "transcript ingestion"),
        _chunk(202, 2, "model.md",      "scoring logic"),
        _chunk(303, 3, "unused.md",     "never cited"),
    ]
    body, citations = generate("how does churn work?", "architecture", chunks)
    assert "[1]" in body and "[2]" in body
    cited_chunk_ids = {c.chunk_id for c in citations}
    assert cited_chunk_ids == {101, 202}
    assert all(c.citation_index in (1, 2) for c in citations)


def test_doc_types_include_all_spec_options():
    expected = {"process", "architecture", "meeting_summary",
                "decision_record", "adr", "release_note"}
    assert expected.issubset(set(DOC_TYPES))


@respx.mock
def test_generate_raises_when_claude_fails():
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )
    with pytest.raises(Exception):
        generate("topic", "process", [_chunk(1, 1, "t", "body")])
