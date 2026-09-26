"""Rebuilding a run's stored price summary from the listings still in the DB.

A run's ``PriceObservation`` is written once, at ingest. When listings are
later hidden or deleted -- by hand, by an exclude phrase, or by an anomaly
action -- the summary goes stale until it is recomputed from what's left.
Every path that hides, unhides or deletes a listing calls
:func:`recompute_for_tracked` (or :func:`delete_tracked_listing`) so the
stored prices never keep counting a listing nobody can see any more.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from orchestrator.api.ingest import lowest_non_auction_price, price_metrics
from orchestrator.models import Listing, PriceObservation, TrackedListing


def recompute_observations(db: Session, vendor_id: str, job_ids: Iterable[str] | None = None) -> int:
    """Rebuild this vendor's per-run price summaries (or just ``job_ids``')
    from the listings still stored, leaving out hidden ones -- directly, via
    their tracked listing, or because that tracked listing was deleted (SQLite
    ignores the FK's ON DELETE SET NULL, so the run's row is left pointing at
    nothing). Runs with no stored listings left (e.g. purged) keep the
    worker's figures, since there is nothing to recompute them from. Returns
    how many observations changed; the caller commits."""
    stmt = select(PriceObservation).where(PriceObservation.vendor_id == vendor_id)
    if job_ids is not None:
        stmt = stmt.where(PriceObservation.job_id.in_(list(job_ids)))
    observations = db.scalars(stmt).all()
    by_job: dict[str, list[tuple[float, str, bool]]] = {}
    for job_id, price, listing_type, hidden, tracked_ref, tracked_id, tracked_hidden in db.execute(
        select(Listing.job_id, Listing.price, Listing.listing_type, Listing.hidden,
               Listing.tracked_listing_id, TrackedListing.id, TrackedListing.hidden)
        .outerjoin(TrackedListing, Listing.tracked_listing_id == TrackedListing.id)
        .where(Listing.job_id.in_([o.job_id for o in observations]))
    ):
        tracked_deleted = tracked_ref is not None and tracked_id is None
        by_job.setdefault(job_id, []).append((price, listing_type, bool(hidden or tracked_hidden or tracked_deleted)))

    changed = 0
    for obs in observations:
        listings = by_job.get(obs.job_id)
        if listings is None:
            continue
        visible = [(price, listing_type) for price, listing_type, hidden in listings if not hidden]
        metrics = price_metrics([price for price, _ in visible])
        metrics["price_low"] = lowest_non_auction_price(visible)
        before = (obs.price_low, obs.price_mean, obs.price_median, obs.listings_kept)
        for field, value in metrics.items():
            setattr(obs, field, value)
        if (obs.price_low, obs.price_mean, obs.price_median, obs.listings_kept) != before:
            changed += 1
    return changed


def _runs_by_vendor(db: Session, job_ids: set[str]) -> dict[str, set[str]]:
    rows = db.execute(
        select(PriceObservation.vendor_id, PriceObservation.job_id)
        .where(PriceObservation.job_id.in_(list(job_ids)))
    ).all() if job_ids else []
    out: dict[str, set[str]] = {}
    for vendor_id, job_id in rows:
        out.setdefault(vendor_id, set()).add(job_id)
    return out


def recompute_jobs(db: Session, job_ids: Iterable[str]) -> int:
    """Recompute the stored prices of just these runs, whatever their vendor."""
    db.flush()
    return sum(recompute_observations(db, vendor_id, jobs)
               for vendor_id, jobs in _runs_by_vendor(db, set(job_ids)).items())


def jobs_listing(db: Session, tracked_ids: Iterable[int]) -> set[str]:
    """Every run a tracked listing was kept in."""
    ids = list(tracked_ids)
    if not ids:
        return set()
    return set(db.scalars(
        select(Listing.job_id).where(Listing.tracked_listing_id.in_(ids)).distinct()
    ).all())


def recompute_for_tracked(db: Session, tracked_ids: Iterable[int]) -> int:
    """After hiding or unhiding tracked listings: recompute every run they
    appeared in, so each run's median and low reflect what's visible."""
    return recompute_jobs(db, jobs_listing(db, tracked_ids))


def delete_tracked_listing(db: Session, tracked: TrackedListing) -> int:
    """Delete a tracked listing (its price history goes with it), take it out
    of every run it was kept in, and recompute those runs -- a deleted listing
    must not keep setting a run's low price.

    The runs' rows are hidden and unlinked rather than deleted: a run whose
    only listing this was would otherwise have no rows left, and a run with no
    rows keeps its worker-reported prices (it looks like a purged run)."""
    job_ids = jobs_listing(db, [tracked.id])
    db.execute(
        update(Listing).where(Listing.tracked_listing_id == tracked.id)
        .values(hidden=True, tracked_listing_id=None)
    )
    db.delete(tracked)
    return recompute_jobs(db, job_ids)
