from publish.bookstack_stub import publish, PublishResult


def test_publish_returns_ok_and_logs(caplog):
    res = publish(title="My doc", body_markdown="# Hi", source_refs=["a.md"])
    assert isinstance(res, PublishResult)
    assert res.ok is True
    assert res.stub_url.startswith("stub://")
