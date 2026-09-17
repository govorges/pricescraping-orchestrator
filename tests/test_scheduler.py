from __future__ import annotations

import json

from orchestrator.db import session_scope
from orchestrator.models import Job, QueryObject, Schedule, Vendor
from orchestrator.scheduler import enqueue_for_schedule

VENDOR_CONFIG = {
    "metadata": {"name": "eBay US"},
    "url": {"start": "https://www.ebay.com/sch/i.html?_nkw="},
    "selectors": {
        "listings": "li.s-card",
        "price": {"css_selector": ".p", "type": "price", "required": True},
    },
}


def _seed():
    with session_scope() as db:
        db.add(Vendor(identifier="usa_ebay", name="eBay US", config=VENDOR_CONFIG))
        db.add(QueryObject(id="q1", content="RTX 5090", minimum_value=500.0))
        db.add(
            Schedule(
                id=1,
                name="hourly",
                vendor_id="usa_ebay",
                query_object_id="q1",
                cron="0 * * * *",
                options={"headless": True},
                enabled=True,
            )
        )


def test_enqueue_for_schedule_enqueues_and_records_job(settings, fake_redis):
    """Firing a schedule enqueues a valid job payload and records a matching Job row, and updates last/next run times."""
    _seed()
    with session_scope() as db:
        schedule = db.get(Schedule, 1)
        job_id, msg_id = enqueue_for_schedule(db, schedule, scheduled_by="scheduler:cron")

    # a message landed on the stream and parses as a worker Job payload
    _stream, entries = fake_redis.xread({settings.job_stream: "0"})[0]
    _mid, fields = entries[0]
    payload = json.loads(fields["payload"])
    assert payload["job_id"] == job_id
    assert payload["scheduled_by"] == "scheduler:cron"

    with session_scope() as db:
        job = db.get(Job, job_id)
        assert job.status == "queued"
        assert job.schedule_id == 1
        assert job.msg_id == msg_id
        schedule = db.get(Schedule, 1)
        assert schedule.last_run_at is not None
        assert schedule.next_run_at is not None
