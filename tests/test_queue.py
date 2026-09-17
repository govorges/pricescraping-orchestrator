from __future__ import annotations

import json

from orchestrator.queue import enqueue, read_deadletter, stream_stats
from orchestrator.schemas import Job


def _payload(job_id="job-1"):
    return {
        "job_id": job_id,
        "attempt": 1,
        "vendor": {
            "identifier": "usa_ebay",
            "url": {"start": "https://x/?q="},
            "selectors": {"listings": "li", "price": {"css_selector": ".p", "type": "price"}},
        },
        "query_object": {"id": "q1", "Content": "RTX 5090"},
        "options": {},
    }


def test_enqueue_writes_parseable_job(settings, fake_redis):
    """A payload written to the stream round-trips as valid JSON that the worker's own Job schema accepts."""
    msg_id = enqueue(_payload(), settings=settings)
    assert msg_id
    _stream, entries = fake_redis.xread({settings.job_stream: "0"})[0]
    _mid, fields = entries[0]
    Job.model_validate(json.loads(fields["payload"]))  # must parse as a worker Job


def test_stream_stats_counts_depth_and_deadletter(settings, fake_redis):
    """Queue depth counts undelivered messages on the main stream; deadletter counts the separate dead stream."""
    enqueue(_payload("a"), settings=settings)
    enqueue(_payload("b"), settings=settings)
    fake_redis.xadd(settings.deadletter_stream, {"payload": "{}", "reason": "boom"})

    stats = stream_stats(settings=settings)
    assert stats["depth"] == 2
    assert stats["deadletter"] == 1


def test_read_deadletter_extracts_job_id(settings, fake_redis):
    """A dead-lettered entry's job_id and reason are pulled out of its stored payload, not left buried in raw JSON."""
    fake_redis.xadd(
        settings.deadletter_stream,
        {"payload": json.dumps({"job_id": "dead-1"}), "reason": "max_attempts_exceeded"},
    )
    rows = read_deadletter(settings=settings)
    assert rows[0]["job_id"] == "dead-1"
    assert rows[0]["reason"] == "max_attempts_exceeded"
