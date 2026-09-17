"""Assemble a worker job payload from panel-managed Vendor + QueryObject rows.

Output shape is exactly ``pricescraping-worker/config/job.example.json``. The
worker stays stateless: the vendor config and query object are embedded as
snapshots at enqueue time.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from orchestrator.models import QueryObject, Vendor
from orchestrator.schemas import Job as JobSchema

_OPTION_KEYS = (
    "engine",
    "headless",
    "nav_timeout_ms",
    "request_delay_ms",
    "max_listings",
    "challenge_retries",
)


def build_job(
    vendor: Vendor,
    query_object: QueryObject,
    *,
    options: dict[str, Any] | None = None,
    scheduled_by: str = "",
    job_id: str | None = None,
) -> dict[str, Any]:
    """Return a validated job payload dict ready for :func:`orchestrator.queue.enqueue`."""
    opts = {k: v for k, v in (options or {}).items() if k in _OPTION_KEYS and v is not None}
    payload: dict[str, Any] = {
        "job_id": job_id or str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "scheduled_by": scheduled_by,
        "attempt": 1,
        "vendor": vendor.snapshot(),
        "query_object": query_object.snapshot(),
        "options": opts,
    }
    # fail fast on a malformed payload, just like scripts/seed_job.py
    JobSchema.model_validate(payload)
    return payload
