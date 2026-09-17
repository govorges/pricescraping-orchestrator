from __future__ import annotations

from datetime import datetime, timedelta, timezone

from orchestrator import dbadmin
from orchestrator.db import session_scope
from orchestrator.models import (
    Job,
    ListingPricePoint,
    PriceObservation,
    TrackedListing,
    Vendor,
)


def _seed_job(db, *, job_id: str, created_at) -> None:
    db.add(
        Job(
            job_id=job_id, vendor_id="v", query_object_id="q", status="ok",
            created_at=created_at,
        )
    )


def test_table_counts_reflects_rows(settings):
    """Row counts reflect what's actually in the DB, and each table is tagged with its correct durability kind."""
    with session_scope() as db:
        db.add(Vendor(identifier="v", name="V", config={}))
    counts = {}
    with session_scope() as db:
        for row in dbadmin.table_counts(db):
            counts[row["table"]] = row
    assert counts["vendor"]["rows"] == 1
    assert counts["vendor"]["kind"] == "config"
    assert counts["tracked_listing"]["kind"] == "durable"
    assert counts["job"]["kind"] == "ephemeral"


def test_sqlite_path_and_backup_roundtrip(settings, tmp_path):
    """A backup can be created, shows up in the listing, and can be deleted again."""
    path = dbadmin.sqlite_path(settings)
    assert path is not None
    assert path.exists()

    backup = dbadmin.create_backup(settings)
    assert backup.size_bytes > 0
    backups = dbadmin.list_backups(settings)
    assert any(b.name == backup.name for b in backups)

    assert dbadmin.delete_backup(settings, backup.name) is True
    assert not any(b.name == backup.name for b in dbadmin.list_backups(settings))


def test_vacuum_runs_without_error(settings):
    """VACUUM against a real sqlite file completes cleanly."""
    dbadmin.vacuum(settings)  # no exception == pass


def test_purge_old_deletes_ephemeral_but_keeps_tracked_listings(settings):
    """Purge removes old jobs/observations but never touches tracked-listing price history, however old."""
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=200)
    with session_scope() as db:
        _seed_job(db, job_id="old", created_at=old)
        _seed_job(db, job_id="new", created_at=now)
        db.add(
            PriceObservation(
                vendor_id="v", query_object_id="q", observed_date=old.date(),
                observed_at=old, job_id="old", status="ok",
            )
        )
        t = TrackedListing(
            vendor_id="v", query_object_id="q", canonical_url="https://x/1",
            first_seen_at=old, last_seen_at=old,
        )
        db.add(t)
        db.flush()
        db.add(ListingPricePoint(tracked_listing_id=t.id, job_id="old", observed_at=old, price=100.0))

    with session_scope() as db:
        result = dbadmin.purge_old(db, older_than_days=90)
    assert result == {"jobs_deleted": 1, "observations_deleted": 1}

    with session_scope() as db:
        assert db.get(Job, "old") is None
        assert db.get(Job, "new") is not None
        assert db.query(PriceObservation).count() == 0
        # durable per-listing history is untouched by the purge
        assert db.query(TrackedListing).count() == 1
        assert db.query(ListingPricePoint).count() == 1
