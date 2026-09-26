"""Read + curate individual listing rows (the per-item detail behind a job's metrics)."""

from __future__ import annotations

from flask import Blueprint, jsonify, request
from sqlalchemy import select

from orchestrator import get_db
from orchestrator.models import Job, Listing
from orchestrator.prices import recompute_jobs

bp = Blueprint("listings", __name__, url_prefix="/api/v1")

_SORTS = {
    "newest": Job.created_at.desc(),
    "price_asc": Listing.price.asc(),
    "price_desc": Listing.price.desc(),
}


def listing_dict(listing: Listing, job: Job | None = None) -> dict:
    return {
        "id": listing.id,
        "job_id": listing.job_id,
        "vendor_id": job.vendor_id if job else None,
        "query_object_id": job.query_object_id if job else None,
        "title": listing.title,
        "link": listing.link,
        "image": listing.image,
        "price": listing.price,
        "extra": listing.extra,
        "hidden": listing.hidden,
        "observed_at": job.created_at.isoformat() if job and job.created_at else None,
    }


@bp.get("/listings")
def list_listings():
    db = get_db()
    stmt = select(Listing, Job).join(Job, Listing.job_id == Job.job_id)
    if qo := request.args.get("query_object_id"):
        stmt = stmt.where(Job.query_object_id == qo)
    if vendor := request.args.get("vendor_id"):
        stmt = stmt.where(Job.vendor_id == vendor)
    show = request.args.get("show", "visible")
    if show == "visible":
        stmt = stmt.where(~Listing.hidden)
    elif show == "hidden":
        stmt = stmt.where(Listing.hidden)
    order = _SORTS.get(request.args.get("sort", "newest"), _SORTS["newest"])
    limit = min(int(request.args.get("limit", 200)), 1000)
    rows = db.execute(stmt.order_by(order).limit(limit)).all()
    return jsonify([listing_dict(listing, job) for listing, job in rows])


@bp.post("/listings/<int:listing_id>/hide")
def hide_listing(listing_id: int):
    db = get_db()
    listing = db.get(Listing, listing_id)
    if listing is None:
        return jsonify({"error": "not found"}), 404
    listing.hidden = True
    recompute_jobs(db, [listing.job_id])
    db.commit()
    return jsonify(listing_dict(listing))


@bp.post("/listings/<int:listing_id>/unhide")
def unhide_listing(listing_id: int):
    db = get_db()
    listing = db.get(Listing, listing_id)
    if listing is None:
        return jsonify({"error": "not found"}), 404
    listing.hidden = False
    recompute_jobs(db, [listing.job_id])
    db.commit()
    return jsonify(listing_dict(listing))


@bp.delete("/listings/<int:listing_id>")
def delete_listing(listing_id: int):
    db = get_db()
    listing = db.get(Listing, listing_id)
    if listing is None:
        return jsonify({"error": "not found"}), 404
    job_id = listing.job_id
    db.delete(listing)
    recompute_jobs(db, [job_id])
    db.commit()
    return "", 204
