"""BookStack publish stub. Real impl is a separate spec.
Logs the payload and returns a fake URL."""
from __future__ import annotations
import logging
from dataclasses import dataclass

log = logging.getLogger("openbrain.publish")


@dataclass
class PublishResult:
    ok: bool
    stub_url: str


def publish(*, title: str, body_markdown: str, source_refs: list[str]) -> PublishResult:
    log.info("BookStack STUB publish: title=%r body_len=%d sources=%d",
             title, len(body_markdown), len(source_refs))
    fake_id = abs(hash(title)) % 1_000_000
    return PublishResult(ok=True, stub_url=f"stub://bookstack/page/{fake_id}")
