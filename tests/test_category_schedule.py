from __future__ import annotations

import json

from orchestrator.db import session_scope
from orchestrator.models import Category, CategorySchedule, Job, QueryObject, Vendor
from orchestrator.scheduler import enqueue_for_category_schedule

VENDOR_CONFIG = {
    "metadata": {"name": "eBay US"},
    "url": {"start": "https://www.ebay.com/sch/i.html?_nkw="},
    "selectors": {
        "listings": "li.s-card",
        "price": {"css_selector": ".p", "type": "price", "required": True},
    },
}


def _seed(*, second_vendor_enabled: bool = True):
    with session_scope() as db:
        db.add(Vendor(identifier="usa_ebay", name="eBay US", config=VENDOR_CONFIG))
        db.add(Vendor(identifier="usa_newegg", name="Newegg US", config=VENDOR_CONFIG,
                      enabled=second_vendor_enabled))
        db.add(Category(id="gpu", name="GPUs"))
        db.add(QueryObject(id="gpu-rtx-5090", content="RTX 5090", category_id="gpu", minimum_value=500.0))
        db.add(QueryObject(id="gpu-rtx-4090", content="RTX 4090", category_id="gpu", minimum_value=400.0))
        db.add(
            CategorySchedule(
                id=1, name="gpu daily", category_id="gpu", vendor_id=None,
                cron="0 9 * * *", options={}, enabled=True,
            )
        )


def test_category_schedule_fans_out_to_every_query_and_enabled_vendor(settings, fake_redis):
    """A category schedule with no pinned vendor enqueues one job per (query, enabled vendor) pair."""
    _seed()
    with session_scope() as db:
        cs = db.get(CategorySchedule, 1)
        jobs = enqueue_for_category_schedule(db, cs, scheduled_by="scheduler:cron")

    # 2 queries x 2 enabled vendors = 4 jobs
    assert len(jobs) == 4
    with session_scope() as db:
        rows = db.query(Job).all()
        assert len(rows) == 4
        assert {j.vendor_id for j in rows} == {"usa_ebay", "usa_newegg"}
        assert {j.query_object_id for j in rows} == {"gpu-rtx-5090", "gpu-rtx-4090"}
        assert all(j.category_schedule_id == 1 for j in rows)
        cs = db.get(CategorySchedule, 1)
        assert cs.last_run_at is not None
        assert cs.next_run_at is not None

    _stream, entries = fake_redis.xread({settings.job_stream: "0"})[0]
    assert len(entries) == 4
    payload = json.loads(entries[0][1]["payload"])
    assert payload["scheduled_by"] == "scheduler:cron"


def test_category_schedule_skips_disabled_vendors(settings, fake_redis):
    """A disabled vendor is excluded from the fan-out entirely, not just skipped with a queued-then-failed job."""
    _seed(second_vendor_enabled=False)
    with session_scope() as db:
        cs = db.get(CategorySchedule, 1)
        jobs = enqueue_for_category_schedule(db, cs, scheduled_by="scheduler:cron")

    assert len(jobs) == 2  # 2 queries x 1 enabled vendor
    with session_scope() as db:
        assert {j.vendor_id for j in db.query(Job).all()} == {"usa_ebay"}


def test_category_schedule_pinned_to_one_vendor(settings, fake_redis):
    """Setting vendor_id on a category schedule restricts the fan-out to just that vendor, not every enabled one."""
    _seed()
    with session_scope() as db:
        db.get(CategorySchedule, 1).vendor_id = "usa_newegg"

    with session_scope() as db:
        cs = db.get(CategorySchedule, 1)
        jobs = enqueue_for_category_schedule(db, cs, scheduled_by="scheduler:cron")

    assert len(jobs) == 2  # 2 queries x the one pinned vendor
    with session_scope() as db:
        assert {j.vendor_id for j in db.query(Job).all()} == {"usa_newegg"}
