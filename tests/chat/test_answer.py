import chat.answer as ca
from retrieval.search import Chunk


def _chunk(cid, doc_id, title, content):
    return Chunk(chunk_id=cid, document_id=doc_id, document_title=title,
                 source_type="local_file", file_extension=".md",
                 content=content, rrf_score=0.5)


def test_answer_returns_body_and_citations(monkeypatch):
    monkeypatch.setattr(ca, "call_claude", lambda system, user: "Churn means X [1]. More [2].")
    chunks = [_chunk(11, 1, "a.md", "churn def"), _chunk(22, 2, "b.md", "more")]
    body, citations = ca.answer("what is churn?", chunks, history=[])
    assert "[1]" in body
    assert {c.chunk_id for c in citations} == {11, 22}


def test_answer_includes_history_in_prompt(monkeypatch):
    captured = {}
    def fake(system, user):
        captured["user"] = user
        return "ok"
    monkeypatch.setattr(ca, "call_claude", fake)
    ca.answer("follow up", [_chunk(1, 1, "a", "x")],
              history=[{"role": "user", "content": "first q"},
                       {"role": "assistant", "content": "first a"}])
    assert "first q" in captured["user"] and "first a" in captured["user"]
