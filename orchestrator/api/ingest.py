"""Result ingest — the sink half of the worker contract.

``POST /api/v1/ingest/scrape-results``
  headers: ``Authorization: Bearer <INGEST_TOKEN>``, ``Idempotency-Key: <job_id>``
  body:    ``pricescraping-worker`` ``ScrapeResult.to_payload()``
  returns: ``202 {"accepted": true}``

Idempotent on ``job_id``: a replay upserts the same ``PriceObservation`` and
replaces the job's ``Listing`` and ``DroppedListing`` rows. See
``pricescraping-worker/worker/reporter.py``.

``price_low`` is recomputed here rather than taken from the worker's metrics:
auction listings are left out, since a current bid isn't a price anyone can
actually buy at (see :func:`lowest_non_auction_price`).

Listings whose condition says they're sold for parts / not working (see
:func:`orchestrator.classify.parts_condition`) are moved to the dropped list
before anything is stored, and the result's price metrics are recomputed
without them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from statistics import mean, median

from flask import Blueprint, current_app, jsonify, request
from pydantic import ValidationError
from sqlalchemy import select

from orchestrator import get_db
from orchestrator.classify import classify_listing_type, parts_condition
from orchestrator.logging import get_logger
from orchestrator.models import (
    DroppedListing,
    Job,
    Listing,
    ListingPricePoint,
    PriceObservation,
    TrackedListing,
)
from orchestrator.schemas import DroppedListingIn, ListingResultIn, ScrapeResultIn
from orchestrator.urlnorm import canonical_url

log = get_logger("orchestrator.ingest")


def _aware_utc(dt: datetime) -> datetime:
    """SQLite round-trips ``DateTime(timezone=True)`` values as naive; normalise
    before comparing against a freshly-parsed (tz-aware) pydantic datetime."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

bp = Blueprint("ingest", __name__)

# Kept in sync with pricescraping-worker/scripts/mock_orchestrator.py
_REQUIRED = {"job_id", "vendor_id", "query_object_id", "observed_date", "status", "metrics"}


def _authorized() -> bool:
    token = current_app.config["SETTINGS"].ingest_token
    header = request.headers.get("Authorization", "")
    return header.startswith("Bearer ") and header[7:] == token


@bp.post("/api/v1/ingest/scrape-results")
def ingest_scrape_result():
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    raw = request.get_json(silent=True)
    if not isinstance(raw, dict):
        return jsonify({"error": "body must be a JSON object"}), 400

    missing = _REQUIRED - raw.keys()
    if missing:
        return jsonify({"error": f"missing fields: {sorted(missing)}"}), 422

    try:
        result = ScrapeResultIn.model_validate(raw)
    except ValidationError as exc:
        return jsonify({"error": "invalid result", "detail": exc.errors(include_url=False)}), 422

    result = _drop_parts_listings(result)
    db = get_db()
    _upsert_observation(db, result)
    _replace_listings(db, result)
    _replace_dropped_listings(db, result)
    _upsert_job(db, result)
    _detect_anomalies(db, result.job_id)
    db.commit()

    log.info(
        "ingested job=%s vendor=%s status=%s kept=%s",
        result.job_id,
        result.vendor_id,
        result.status,
        result.metrics.listings_kept,
    )
    return jsonify({"accepted": True}), 202


def _detect_anomalies(db, job_id: str) -> None:
    """Run the anomaly checks for this run inside a savepoint: a bug in a
    check must never cost us the scrape result itself."""
    from orchestrator.anomalies import detect_for_run  # imports this module

    db.flush()
    try:
        with db.begin_nested():
            detect_for_run(db, job_id)
    except Exception:  # noqa: BLE001
        log.exception("anomaly detection failed for job=%s", job_id)


def parts_drop_reason(condition: str) -> str:
    return f"condition_for_parts: {condition!r}"


def price_metrics(prices: list[float]) -> dict:
    """``listings_kept`` / low / mean / median for a run's kept prices, computed
    the way the worker does (``pricescraping-worker/worker/metrics.py``)."""
    if not prices:
        return {"listings_kept": 0, "price_low": None, "price_mean": None, "price_median": None}
    return {
        "listings_kept": len(prices),
        "price_low": round(min(prices), 2),
        "price_mean": round(mean(prices), 2),
        "price_median": round(median(prices), 2),
    }


def _drop_parts_listings(result: ScrapeResultIn) -> ScrapeResultIn:
    """Move for-parts / not-working listings to ``dropped`` and recompute the
    metrics from what's left. Untouched when there's nothing to drop, so the
    worker's own metrics stand."""
    kept, dropped = [], []
    for item in result.listings:
        condition = parts_condition(item.extra)
        if condition is None:
            kept.append(item)
        else:
            dropped.append(DroppedListingIn(title=item.title, reason=parts_drop_reason(condition)))
    if not dropped:
        return result
    metrics = result.metrics.model_copy(update=price_metrics([item.price for item in kept]))
    return result.model_copy(
        update={"listings": kept, "dropped": [*result.dropped, *dropped], "metrics": metrics}
    )


def lowest_non_auction_price(listings) -> float | None:
    """Lowest price among ``(price, listing_type)`` pairs, skipping auctions.
    None when there is nothing but auctions (or nothing at all)."""
    prices = [price for price, listing_type in listings if listing_type != "auction"]
    return min(prices) if prices else None


def _price_low(result: ScrapeResultIn) -> float | None:
    # Without listings there is nothing to filter, so the worker's own figure
    # is the best available.
    if not result.listings:
        return result.metrics.price_low
    return lowest_non_auction_price(
        (item.price, classify_listing_type(item.title, item.extra)) for item in result.listings
    )


def _upsert_observation(db, result: ScrapeResultIn) -> None:
    obs = db.scalar(
        select(PriceObservation).where(
            PriceObservation.vendor_id == result.vendor_id,
            PriceObservation.query_object_id == result.query_object_id,
            PriceObservation.observed_date == result.observed_date,
            PriceObservation.job_id == result.job_id,
        )
    )
    if obs is None:
        obs = PriceObservation(
            vendor_id=result.vendor_id,
            query_object_id=result.query_object_id,
            observed_date=result.observed_date,
            job_id=result.job_id,
        )
        db.add(obs)
    obs.observed_at = result.observed_at
    obs.status = result.status
    obs.price_low = _price_low(result)
    obs.price_mean = result.metrics.price_mean
    obs.price_median = result.metrics.price_median
    obs.listings_found = result.metrics.listings_found
    obs.listings_kept = result.metrics.listings_kept


def _replace_listings(db, result: ScrapeResultIn) -> None:
    for row in db.scalars(select(Listing).where(Listing.job_id == result.job_id)).all():
        db.delete(row)
    for item in result.listings:
        listing_type = classify_listing_type(item.title, item.extra)
        tracked = _track_listing(db, result, item, listing_type)
        db.add(
            Listing(
                job_id=result.job_id,
                title=item.title,
                link=item.link,
                image=item.image,
                price=item.price,
                extra=item.extra,
                listing_type=listing_type,
                tracked_listing_id=tracked.id if tracked else None,
            )
        )


def _replace_dropped_listings(db, result: ScrapeResultIn) -> None:
    for row in db.scalars(
        select(DroppedListing).where(DroppedListing.job_id == result.job_id)
    ).all():
        db.delete(row)
    for item in result.dropped:
        db.add(DroppedListing(job_id=result.job_id, title=item.title, reason=item.reason))


def _track_listing(
    db, result: ScrapeResultIn, item: ListingResultIn, listing_type: str
) -> TrackedListing | None:
    """Resolve (or create) the canonical identity behind ``item.link`` and record
    one price point for this job. Returns ``None`` when the listing has no link
    to canonicalise — it still gets stored on the job, just not tracked over time.
    """
    canonical = canonical_url(item.link)
    if canonical is None:
        return None

    tracked = db.scalar(
        select(TrackedListing).where(
            TrackedListing.vendor_id == result.vendor_id,
            TrackedListing.canonical_url == canonical,
        )
    )
    if tracked is None:
        tracked = TrackedListing(
            vendor_id=result.vendor_id,
            query_object_id=result.query_object_id,
            canonical_url=canonical,
            link=item.link,
            title=item.title,
            image=item.image,
            listing_type=listing_type,
            first_price=item.price,
            last_price=item.price,
            first_seen_at=result.observed_at,
            last_seen_at=result.observed_at,
        )
        db.add(tracked)
        db.flush()  # assign tracked.id before it's referenced below
    else:
        tracked.query_object_id = result.query_object_id
        tracked.link = item.link
        tracked.title = item.title or tracked.title
        tracked.image = item.image or tracked.image
        if listing_type != "unknown":
            tracked.listing_type = listing_type
        tracked.last_price = item.price
        if result.observed_at >= _aware_utc(tracked.last_seen_at):
            tracked.last_seen_at = result.observed_at

    exists = db.scalar(
        select(ListingPricePoint).where(
            ListingPricePoint.tracked_listing_id == tracked.id,
            ListingPricePoint.job_id == result.job_id,
        )
    )
    if exists is None:
        db.add(
            ListingPricePoint(
                tracked_listing_id=tracked.id,
                job_id=result.job_id,
                observed_at=result.observed_at,
                price=item.price,
                title=item.title,
                extra=item.extra,
            )
        )
        tracked.observations_count = (tracked.observations_count or 0) + 1
    return tracked


def _upsert_job(db, result: ScrapeResultIn) -> None:
    job = db.get(Job, result.job_id)
    if job is None:
        job = Job(
            job_id=result.job_id,
            vendor_id=result.vendor_id,
            query_object_id=result.query_object_id,
            created_at=datetime.now(timezone.utc),
        )
        db.add(job)
    job.status = result.status
    job.result_at = result.observed_at
    job.duration_ms = result.duration_ms
    node_id = (result.worker or {}).get("node")
    if node_id:
        job.node_id = node_id
    if result.error is not None:
        job.error_type = result.error.type
        job.error_message = result.error.message
    else:
        job.error_type = None
        job.error_message = None
