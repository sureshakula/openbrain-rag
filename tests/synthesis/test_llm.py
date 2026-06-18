import respx
import httpx
from synthesis.llm import call_claude, map_citations, Citation
from retrieval.search import Chunk


def _chunk(cid, doc_id, title, content):
    return Chunk(chunk_id=cid, document_id=doc_id, document_title=title,
                 source_type="local_file", file_extension=".md",
                 content=content, rrf_score=0.5)


@respx.mock
def test_call_claude_returns_text():
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(200, json={
            "content": [{"type": "text", "text": "hello [1]"}],
            "model": "x", "usage": {"input_tokens": 1, "output_tokens": 1},
        })
    )
    out = call_claude("sys", "user prompt")
    assert out == "hello [1]"


def test_map_citations_maps_and_filters():
    chunks = [_chunk(101, 1, "a.md", "x"), _chunk(202, 2, "b.md", "y")]
    cites = map_citations("uses [1] and [2] and [9]", chunks)
    assert [c.chunk_id for c in cites] == [101, 202]
    assert all(isinstance(c, Citation) for c in cites)
    assert [c.citation_index for c in cites] == [1, 2]
