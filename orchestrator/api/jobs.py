"""Read-only views over job history, listings, and the price time-series."""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import select

from orchestrator import get_db
from orchestrator.models import DroppedListing, Job, Listing, PriceObservation, QueryObject
from orchestrator.queue import delete_message, purge_queue
from orchestrator.timeutil import iso_utc

bp = Blueprint("jobs", __name__, url_prefix="/api/v1")

# statuses that mean "still on the queue, no worker has finished it"
OPEN_STATUSES = ("queued", "running")


def job_dict(j: Job) -> dict:
    return {
        "job_id": j.job_id,
        "schedule_id": j.schedule_id,
        "vendor_id": j.vendor_id,
        "query_object_id": j.query_object_id,
        "status": j.status,
        "attempt": j.attempt,
        "scheduled_by": j.scheduled_by,
        "msg_id": j.msg_id,
        "created_at": iso_utc(j.created_at),
        "result_at": iso_utc(j.result_at),
        "duration_ms": j.duration_ms,
        "error_type": j.error_type,
        "error_message": j.error_message,
    }


def listing_dict(row: Listing) -> dict:
    return {
        "title": row.title,
        "link": row.link,
        "image": row.image,
        "price": row.price,
        "extra": row.extra,
    }


def dropped_dict(row: DroppedListing) -> dict:
    return {"title": row.title, "reason": row.reason}


@bp.get("/jobs")
def list_jobs():
    db = get_db()
    limit = min(int(request.args.get("limit", 50)), 500)
    offset = int(request.args.get("offset", 0))
    stmt = select(Job).order_by(Job.created_at.desc())
    if status := request.args.get("status"):
        stmt = stmt.where(Job.status == status)
    if vendor := request.args.get("vendor_id"):
        stmt = stmt.where(Job.vendor_id == vendor)
    rows = db.scalars(stmt.limit(limit).offset(offset)).all()
    return jsonify([job_dict(j) for j in rows])


@bp.post("/jobs/<job_id>/cancel")
def cancel_job(job_id: str):
    """Pull a still-queued job off the stream and mark it ``canceled``."""
    db = get_db()
    j = db.get(Job, job_id)
    if j is None:
        return jsonify({"error": "not found"}), 404
    if j.status not in OPEN_STATUSES:
        return jsonify({"error": f"job is already {j.status!r}, nothing to cancel"}), 409
    removed = 0
    if j.msg_id:
        removed = delete_message(j.msg_id, settings=current_app.config["SETTINGS"])
    j.status = "canceled"
    db.commit()
    return jsonify({**job_dict(j), "removed_from_stream": removed})


@bp.post("/queue/purge")
def purge():
    """Drop every undelivered job from the stream; mark those rows ``canceled``."""
    settings = current_app.config["SETTINGS"]
    db = get_db()
    removed = purge_queue(settings=settings)
    canceled = 0
    for j in db.scalars(select(Job).where(Job.status.in_(OPEN_STATUSES))).all():
        j.status = "canceled"
        canceled += 1
    db.commit()
    return jsonify({"removed_from_stream": removed, "jobs_canceled": canceled})


@bp.get("/jobs/<job_id>")
def get_job(job_id: str):
    db = get_db()
    j = db.get(Job, job_id)
    if j is None:
        return jsonify({"error": "not found"}), 404
    listings = db.scalars(select(Listing).where(Listing.job_id == job_id)).all()
    dropped = db.scalars(select(DroppedListing).where(DroppedListing.job_id == job_id)).all()
    obs = db.scalars(
        select(PriceObservation)
        .where(PriceObservation.job_id == job_id)
        .order_by(PriceObservation.observed_at.desc())
    ).first()
    return jsonify(
        {
            **job_dict(j),
            "listings": [listing_dict(x) for x in listings],
            "dropped": [dropped_dict(x) for x in dropped],
            "observation": _obs_dict(obs) if obs else None,
        }
    )


def _obs_dict(o: PriceObservation) -> dict:
    return {
        "observed_at": iso_utc(o.observed_at),
        "observed_date": o.observed_date.isoformat() if o.observed_date else None,
        "vendor_id": o.vendor_id,
        "query_object_id": o.query_object_id,
        "job_id": o.job_id,
        "status": o.status,
        "price_low": o.price_low,
        "price_mean": o.price_mean,
        "price_median": o.price_median,
        "listings_found": o.listings_found,
        "listings_kept": o.listings_kept,
    }


@bp.get("/observations")
def list_observations():
    """Rows for the dashboard price chart. Filter by vendor_id / query_object_id / category_id."""
    db = get_db()
    stmt = select(PriceObservation).order_by(PriceObservation.observed_at.asc())
    if vendor := request.args.get("vendor_id"):
        stmt = stmt.where(PriceObservation.vendor_id == vendor)
    if qo := request.args.get("query_object_id"):
        stmt = stmt.where(PriceObservation.query_object_id == qo)
    if cat := request.args.get("category_id"):
        qids = db.scalars(select(QueryObject.id).where(QueryObject.category_id == cat)).all()
        stmt = stmt.where(PriceObservation.query_object_id.in_(qids or ["__none__"]))
    limit = min(int(request.args.get("limit", 1000)), 5000)
    rows = db.scalars(stmt.limit(limit)).all()
    return jsonify([_obs_dict(o) for o in rows])
