"""Two dashboard/panel bugs this covers:

1. A node's "jobs / min" always read '-' because nothing ever wrote
   ``Node.meta['jobs_per_min']``. It's now computed from ``Job.node_id`` /
   ``Job.result_at`` directly (see :func:`_jobs_per_min_by_node`).
2. The Query objects list's "median · N obs" is now "median · N active
   listings" -- the count of currently-active tracked listings, not a raw
   observation count (see :func:`_active_listing_counts`).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from orchestrator.models import Job, PriceObservation, TrackedListing
from orchestrator.ui.views import _active_listing_counts, _job_counts_since, _jobs_per_min_by_node

VENDOR = "usa_ebay"
QID = "gpu-rtx-5090"


def _job(db, *, job_id, node_id, status="ok", created_at=None, result_at=None):
    now = datetime.now(timezone.utc)
    db.add(
        Job(
            job_id=job_id, vendor_id=VENDOR, query_object_id=QID, node_id=node_id,
            status=status, created_at=created_at or now, result_at=result_at,
        )
    )


def test_jobs_per_min_by_node_uses_recent_window_only(db_session):
    """Only jobs completed within the trailing 5-minute window count toward a node's rate; unattributed jobs are excluded."""
    now = datetime.now(timezone.utc)
    _job(db_session, job_id="j1", node_id="local-abc", result_at=now - timedelta(minutes=1))
    _job(db_session, job_id="j2", node_id="local-abc", result_at=now - timedelta(minutes=2))
    _job(db_session, job_id="j3", node_id="local-abc", result_at=now - timedelta(minutes=30))  # too old
    _job(db_session, job_id="j4", node_id="local-other", result_at=now - timedelta(seconds=10))
    _job(db_session, job_id="j5", node_id=None, result_at=now)  # never attributed to a node
    db_session.flush()

    rates = _jobs_per_min_by_node(db_session)

    assert rates["local-abc"] == 0.4  # 2 jobs / 5-minute window
    assert rates["local-other"] == 0.2
    assert None not in rates


def test_job_counts_since_groups_by_status(db_session):
    """Counts are grouped by status and scoped to the given cutoff -- an older job is excluded."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=24)
    _job(db_session, job_id="a", node_id="n1", status="ok", created_at=now)
    _job(db_session, job_id="b", node_id="n1", status="ok", created_at=now)
    _job(db_session, job_id="c", node_id="n1", status="error", created_at=now)
    _job(db_session, job_id="old", node_id="n1", status="ok", created_at=now - timedelta(hours=48))
    db_session.flush()

    counts = _job_counts_since(db_session, since)

    assert counts == {"ok": 2, "error": 1}


def _tracked(db, *, canonical_url, query_object_id=QID, last_seen_at, hidden=False):
    t = TrackedListing(
        vendor_id=VENDOR, query_object_id=query_object_id, canonical_url=canonical_url,
        first_seen_at=last_seen_at, last_seen_at=last_seen_at, hidden=hidden,
    )
    db.add(t)
    db.flush()
    return t


def _obs(db, *, observed_at, status="ok"):
    db.add(
        PriceObservation(
            vendor_id=VENDOR, query_object_id=QID, observed_date=observed_at.date(),
            observed_at=observed_at, job_id=f"j-{observed_at.timestamp()}", status=status,
        )
    )


def test_active_listing_counts_excludes_ended_and_hidden(db_session):
    """A listing missing from the last two clean runs doesn't count as active, and hidden listings never count at all."""
    now = datetime.now(timezone.utc)
    earlier_run = now - timedelta(days=2)

    # seen in the oldest run only -> missing from the last two clean runs -> ended
    _tracked(db_session, canonical_url="https://x/ended", last_seen_at=earlier_run)
    # seen in the latest clean run -> still active
    _tracked(db_session, canonical_url="https://x/still-active", last_seen_at=now)
    # would count as active, but hidden listings are excluded entirely
    _tracked(db_session, canonical_url="https://x/hidden", last_seen_at=now, hidden=True)

    _obs(db_session, observed_at=earlier_run)
    _obs(db_session, observed_at=now - timedelta(days=1))
    _obs(db_session, observed_at=now)  # the latest trustworthy run
    db_session.flush()

    counts = _active_listing_counts(db_session)

    assert counts.get(QID, 0) == 1
