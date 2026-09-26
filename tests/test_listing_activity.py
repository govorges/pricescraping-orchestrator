"""A listing is marked 'ended / sold' when it's missing from its vendor's two
most recent *trustworthy* scrape runs (one miss isn't enough -- search results
shuffle between runs). A run that errored — including a blocked or
challenge-page scrape that came back with zero listings — proves nothing about
what's still listed, so it must never be held against a listing's absence.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from orchestrator.models import PriceObservation, TrackedListing
from orchestrator.ui.views import _annotate_activity, _run_times_by_vendor

VENDOR = "usa_ebay"
QID = "gpu-rtx-5090"


def _obs(db, *, observed_at, status, job_id):
    db.add(
        PriceObservation(
            vendor_id=VENDOR, query_object_id=QID, observed_date=observed_at.date(),
            observed_at=observed_at, job_id=job_id, status=status,
            listings_found=0, listings_kept=0,
        )
    )


def _tracked(db, *, canonical_url, last_seen_at):
    t = TrackedListing(
        vendor_id=VENDOR, query_object_id=QID, canonical_url=canonical_url,
        first_seen_at=last_seen_at, last_seen_at=last_seen_at, listing_type="buy_now",
    )
    db.add(t)
    db.flush()
    return t


def test_error_run_does_not_mark_listing_ended(db_session):
    """A listing absent only from a subsequent error/challenge run is still considered active."""
    now = datetime.now(timezone.utc)
    last_ok = now - timedelta(days=2)

    tracked = _tracked(db_session, canonical_url="https://x/1", last_seen_at=last_ok)
    _obs(db_session, observed_at=last_ok, status="ok", job_id="j0")
    _obs(db_session, observed_at=now, status="error", job_id="j1")  # e.g. ChallengePage
    db_session.flush()

    runs = _run_times_by_vendor(db_session, QID)
    _annotate_activity([tracked], runs)

    assert tracked.active is True
    assert tracked.runs_missed == 0


def test_multiple_error_runs_still_dont_mark_it_ended(db_session):
    """Any number of consecutive error runs in a row still don't count against a listing -- not just a single one."""
    now = datetime.now(timezone.utc)
    last_ok = now - timedelta(days=5)

    tracked = _tracked(db_session, canonical_url="https://x/1", last_seen_at=last_ok)
    _obs(db_session, observed_at=last_ok, status="ok", job_id="j0")
    for i, delta in enumerate((4, 3, 2, 1)):
        _obs(db_session, observed_at=now - timedelta(days=delta), status="error", job_id=f"e{i}")
    db_session.flush()

    runs = _run_times_by_vendor(db_session, QID)
    _annotate_activity([tracked], runs)

    assert tracked.active is True


def test_one_clean_miss_is_not_enough_to_mark_it_ended(db_session):
    """Missing from a single clean run is common while a listing is still for sale -- it stays active."""
    now = datetime.now(timezone.utc)
    last_ok = now - timedelta(days=2)

    tracked = _tracked(db_session, canonical_url="https://x/1", last_seen_at=last_ok)
    _obs(db_session, observed_at=last_ok, status="ok", job_id="j0")
    _obs(db_session, observed_at=now, status="ok", job_id="j1")  # ran clean, listing wasn't in it
    db_session.flush()

    runs = _run_times_by_vendor(db_session, QID)
    _annotate_activity([tracked], runs)

    assert tracked.active is True
    assert tracked.runs_missed == 1


def test_two_clean_runs_without_the_listing_mark_it_ended(db_session):
    """Two clean ('ok') runs in a row that don't include the listing are real evidence it ended/sold."""
    now = datetime.now(timezone.utc)
    last_ok = now - timedelta(days=2)

    tracked = _tracked(db_session, canonical_url="https://x/1", last_seen_at=last_ok)
    _obs(db_session, observed_at=last_ok, status="ok", job_id="j0")
    _obs(db_session, observed_at=now - timedelta(days=1), status="ok", job_id="j1")
    _obs(db_session, observed_at=now, status="ok", job_id="j2")
    db_session.flush()

    runs = _run_times_by_vendor(db_session, QID)
    _annotate_activity([tracked], runs)

    assert tracked.active is False
    assert tracked.runs_missed == 2


def test_partial_run_is_still_a_trustworthy_run(db_session):
    """'partial' means selectors/challenge trouble but some real listings still
    came back, so it's still evidence — only a full 'error' is excluded."""
    now = datetime.now(timezone.utc)
    last_ok = now - timedelta(days=2)

    tracked = _tracked(db_session, canonical_url="https://x/1", last_seen_at=last_ok)
    _obs(db_session, observed_at=last_ok, status="ok", job_id="j0")
    _obs(db_session, observed_at=now - timedelta(days=1), status="partial", job_id="j1")
    _obs(db_session, observed_at=now, status="partial", job_id="j2")
    db_session.flush()

    runs = _run_times_by_vendor(db_session, QID)
    _annotate_activity([tracked], runs)

    assert tracked.active is False
