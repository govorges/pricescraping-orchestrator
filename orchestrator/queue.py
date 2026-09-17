"""Redis Streams producer + observability.

The orchestrator is a *producer* on ``JOB_STREAM`` and a *reader* of the
dead-letter stream. It never joins the ``workers`` consumer group — that belongs
to the workers (``pricescraping-worker/worker/consumer.py``).
"""

from __future__ import annotations

import json
from typing import Any

import redis

from orchestrator.logging import get_logger
from orchestrator.settings import Settings, get_settings

log = get_logger("orchestrator.queue")


def build_redis(settings: Settings | None = None) -> redis.Redis:
    settings = settings or get_settings()
    return redis.Redis.from_url(settings.redis_url, decode_responses=True)


def enqueue(payload: dict[str, Any], *, client: redis.Redis | None = None,
            settings: Settings | None = None) -> str:
    """``XADD`` a job payload. Returns the stream message id."""
    settings = settings or get_settings()
    client = client or build_redis(settings)
    msg_id = client.xadd(settings.job_stream, {"payload": json.dumps(payload)})
    log.info("enqueued job %s as %s", payload.get("job_id"), msg_id)
    return msg_id


def delete_message(msg_id: str, *, client: redis.Redis | None = None,
                   settings: Settings | None = None) -> int:
    """``XDEL`` one entry from the job stream. Returns how many were removed (0 or 1).

    A worker that has already claimed the entry keeps its pending reference, but
    ``XREADGROUP`` will never hand a deleted entry to a consumer.
    """
    settings = settings or get_settings()
    client = client or build_redis(settings)
    try:
        return int(client.xdel(settings.job_stream, msg_id) or 0)
    except redis.ResponseError:
        return 0


def purge_queue(*, client: redis.Redis | None = None, settings: Settings | None = None) -> int:
    """Remove every undelivered entry from the job stream. Returns the count removed."""
    settings = settings or get_settings()
    client = client or build_redis(settings)
    try:
        ids = [mid for mid, _ in client.xrange(settings.job_stream)]
    except redis.ResponseError:
        return 0
    if not ids:
        return 0
    return int(client.xdel(settings.job_stream, *ids) or 0)


def stream_stats(*, client: redis.Redis | None = None, settings: Settings | None = None) -> dict:
    """Queue depth / consumer group / pending / dead-letter snapshot for the panel."""
    settings = settings or get_settings()
    client = client or build_redis(settings)

    try:
        depth = client.xlen(settings.job_stream)
    except redis.ResponseError:
        depth = 0

    groups: list[dict] = []
    consumers = 0
    pending = 0
    try:
        for g in client.xinfo_groups(settings.job_stream):
            groups.append(
                {
                    "name": g.get("name"),
                    "consumers": g.get("consumers", 0),
                    "pending": g.get("pending", 0),
                    "lag": g.get("lag"),
                }
            )
            consumers += int(g.get("consumers", 0) or 0)
            pending += int(g.get("pending", 0) or 0)
    except redis.ResponseError:
        pass

    try:
        deadletter = client.xlen(settings.deadletter_stream)
    except redis.ResponseError:
        deadletter = 0

    return {
        "stream": settings.job_stream,
        "depth": depth,
        "pending": pending,
        "consumers": consumers,
        "groups": groups,
        "deadletter": deadletter,
    }


def read_deadletter(count: int = 50, *, client: redis.Redis | None = None,
                    settings: Settings | None = None) -> list[dict]:
    """Most-recent-first dead-letter entries. Each item: id / reason / job_id / payload."""
    settings = settings or get_settings()
    client = client or build_redis(settings)
    out: list[dict] = []
    try:
        entries = client.xrevrange(settings.deadletter_stream, count=count)
    except redis.ResponseError:
        return out
    for msg_id, fields in entries:
        payload_raw = fields.get("payload", "")
        job_id = None
        try:
            job_id = json.loads(payload_raw).get("job_id")
        except (json.JSONDecodeError, AttributeError):
            pass
        out.append(
            {
                "id": msg_id,
                "reason": fields.get("reason", ""),
                "original_id": fields.get("original_id", ""),
                "job_id": job_id,
                "payload": payload_raw,
            }
        )
    return out
