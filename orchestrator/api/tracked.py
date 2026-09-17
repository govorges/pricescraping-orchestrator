"""Read access to tracked listings — a single real-world item followed across
scrapes by its canonicalised URL, with a price-over-time history."""

from __future__ import annotations

from flask import Blueprint, jsonify, request
from sqlalchemy import select

from orchestrator import get_db
from orchestrator.models import TrackedListing
from orchestrator.timeutil import iso_utc

bp = Blueprint("tracked", __name__, url_prefix="/api/v1/tracked-listings")

_SORTS = {
    "last_seen": TrackedListing.last_seen_at.desc(),
    "first_seen": TrackedListing.first_seen_at.desc(),
    "price_asc": TrackedListing.last_price.asc(),
    "price_desc": TrackedListing.last_price.desc(),
}


def tracked_dict(t: TrackedListing, *, with_history: bool = False) -> dict:
    out = {
        "id": t.id,
        "vendor_id": t.vendor_id,
        "query_object_id": t.query_object_id,
        "canonical_url": t.canonical_url,
        "link": t.link,
        "title": t.title,
        "image": t.image,
        "listing_type": t.listing_type,
        "first_price": t.first_price,
        "last_price": t.last_price,
        "price_change_pct": t.price_change_pct,
        "first_seen_at": iso_utc(t.first_seen_at),
        "last_seen_at": iso_utc(t.last_seen_at),
        "observations_count": t.observations_count,
    }
    if with_history:
        out["history"] = [
            {
                "observed_at": iso_utc(p.observed_at),
                "price": p.price,
                "job_id": p.job_id,
                "title": p.title,
            }
            for p in t.price_points
        ]
    return out


@bp.get("")
def list_tracked():
    db = get_db()
    stmt = select(TrackedListing)
    if vendor := request.args.get("vendor_id"):
        stmt = stmt.where(TrackedListing.vendor_id == vendor)
    if qo := request.args.get("query_object_id"):
        stmt = stmt.where(TrackedListing.query_object_id == qo)
    if lt := request.args.get("listing_type"):
        stmt = stmt.where(TrackedListing.listing_type == lt)
    order = _SORTS.get(request.args.get("sort", "last_seen"), _SORTS["last_seen"])
    limit = min(int(request.args.get("limit", 200)), 1000)
    rows = db.scalars(stmt.order_by(order).limit(limit)).all()
    return jsonify([tracked_dict(t) for t in rows])


@bp.get("/<int:tracked_id>")
def get_tracked(tracked_id: int):
    t = get_db().get(TrackedListing, tracked_id)
    if t is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(tracked_dict(t, with_history=True))
