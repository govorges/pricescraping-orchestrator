"""Product hub: the product list plus one page per query object with Overview,
Listings and Settings tabs.

Replaces the separate Query objects, Insights and per-query Listings pages,
which all showed a slice of the same thing; their old URLs redirect here (see
``orchestrator.ui.views`` and ``orchestrator.ui.insights``). The create/update/
delete form posts still live in ``views`` -- only where they land changed.
"""

from __future__ import annotations

from flask import Blueprint, abort, render_template, request
from sqlalchemy import case, func, select

from orchestrator import get_db
from orchestrator.models import (
    Anomaly,
    Category,
    CategorySchedule,
    Job,
    PriceObservation,
    QueryObject,
    Schedule,
    TrackedListing,
)
from orchestrator.bands import bands_by_vendor
from orchestrator.ui.insights import _leaderboard, _query_insights
from orchestrator.ui.views import (
    TRACKED_LISTING_SORTS,
    _annotate_activity,
    _query_price_stats,
    _run_times_by_vendor,
    _vendor_map,
)

bp = Blueprint("products", __name__, template_folder="templates")


def _run_states(db, qids: list[str]) -> dict[str, dict]:
    """Per product: its most recent job, how many of its latest runs came back
    empty (a sign the vendor's selectors drifted), and whether anything
    schedules it -- directly or through its category."""
    scheduled = {
        qid for (qid,) in db.execute(
            select(Schedule.query_object_id).where(Schedule.enabled).distinct()
        ).all()
    }
    scheduled_cats = {
        cid for (cid,) in db.execute(
            select(CategorySchedule.category_id).where(CategorySchedule.enabled).distinct()
        ).all()
    }
    cat_of = dict(db.execute(select(QueryObject.id, QueryObject.category_id)).all())
    out = {}
    for qid in qids:
        last_job = db.scalars(
            select(Job).where(Job.query_object_id == qid).order_by(Job.created_at.desc()).limit(1)
        ).first()
        recent = db.scalars(
            select(PriceObservation).where(PriceObservation.query_object_id == qid)
            .order_by(PriceObservation.observed_at.desc()).limit(2)
        ).all()
        out[qid] = {
            "job": last_job,
            "zero_runs": len(recent) if recent and all((o.listings_kept or 0) == 0 for o in recent) else 0,
            "scheduled": qid in scheduled or cat_of.get(qid) in scheduled_cats,
        }
    return out


def _product_or_404(db, qid: str) -> QueryObject:
    q = db.get(QueryObject, qid)
    if q is None:
        abort(404)
    return q


def _schedules_for(db, q: QueryObject) -> tuple[list[Schedule], list[CategorySchedule]]:
    direct = db.scalars(
        select(Schedule).where(Schedule.query_object_id == q.id).order_by(Schedule.id)
    ).all()
    via_category = (
        db.scalars(
            select(CategorySchedule).where(CategorySchedule.category_id == q.category_id)
            .order_by(CategorySchedule.id)
        ).all()
        if q.category_id
        else []
    )
    return list(direct), list(via_category)


def _active_listing_count(db, qid: str) -> int:
    rows = db.scalars(
        select(TrackedListing).where(TrackedListing.query_object_id == qid, ~TrackedListing.hidden)
    ).all()
    _annotate_activity(rows, _run_times_by_vendor(db, qid))
    return sum(1 for t in rows if t.active)


def _frame(db, q: QueryObject, tab: str) -> dict:
    """What the shared product header and tab strip (``_product.html``) need."""
    direct, via_category = _schedules_for(db, q)
    open_anomalies = db.scalars(
        select(Anomaly).where(Anomaly.query_object_id == q.id, Anomaly.status == "open")
        .order_by(case({"high": 0, "med": 1}, value=Anomaly.severity, else_=2), Anomaly.detected_at.desc())
    ).all()
    return {
        "q": q,
        "tab": tab,
        "vmap": _vendor_map(db),
        "product": {
            "active_listings": _active_listing_count(db, q.id),
            "schedules_enabled": sum(1 for s in [*direct, *via_category] if s.enabled),
            "open_anomalies": open_anomalies,
        },
    }


# ---- pages ------------------------------------------------------------


@bp.get("/products")
def products_list():
    db = get_db()
    rows = _leaderboard(db)
    return render_template(
        "products.html",
        rows=rows,
        queries={q.id: q for q in db.scalars(select(QueryObject)).all()},
        run_state=_run_states(db, [r["id"] for r in rows]),
        categories=db.scalars(select(Category).order_by(Category.id)).all(),
        category_id=request.args.get("category_id", ""),
    )


@bp.get("/products/<qid>")
def overview(qid: str):
    db = get_db()
    q = _product_or_404(db, qid)
    return render_template("product_overview.html", **_frame(db, q, "overview"), **_query_insights(db, q))


@bp.get("/products/<qid>/listings")
def listings(qid: str):
    db = get_db()
    q = _product_or_404(db, qid)

    stmt = select(TrackedListing).where(TrackedListing.query_object_id == qid)
    if vendor := request.args.get("vendor_id"):
        stmt = stmt.where(TrackedListing.vendor_id == vendor)
    if listing_type := request.args.get("listing_type"):
        stmt = stmt.where(TrackedListing.listing_type == listing_type)
    show = request.args.get("show", "visible")
    if show == "visible":
        stmt = stmt.where(~TrackedListing.hidden)
    elif show == "hidden":
        stmt = stmt.where(TrackedListing.hidden)
    order = TRACKED_LISTING_SORTS.get(request.args.get("sort", "last_seen"),
                                      TRACKED_LISTING_SORTS["last_seen"])
    rows = list(db.scalars(stmt.order_by(order).limit(500)).all())

    runs_by_vendor = _run_times_by_vendor(db, qid)
    _annotate_activity(rows, runs_by_vendor)

    status = request.args.get("status", "all")
    if status == "active":
        rows = [t for t in rows if t.active]
    elif status == "inactive":
        rows = [t for t in rows if not t.active]

    hide_counts = dict(
        db.execute(
            select(TrackedListing.hidden, func.count())
            .where(TrackedListing.query_object_id == qid)
            .group_by(TrackedListing.hidden)
        ).all()
    )
    all_rows = db.scalars(
        select(TrackedListing).where(TrackedListing.query_object_id == qid, ~TrackedListing.hidden)
    ).all()
    _annotate_activity(all_rows, runs_by_vendor)
    counts = {
        "visible": hide_counts.get(False, 0),
        "hidden": hide_counts.get(True, 0),
        "active": sum(1 for t in all_rows if t.active),
        "inactive": sum(1 for t in all_rows if not t.active),
    }
    vendors_used = sorted(
        vid for (vid,) in db.execute(
            select(TrackedListing.vendor_id).where(TrackedListing.query_object_id == qid).distinct()
        ).all()
    )
    return render_template(
        "product_listings.html",
        **_frame(db, q, "listings"),
        rows=rows,
        vendors_used=vendors_used,
        counts=counts,
        args=request.args,
    )


@bp.get("/products/<qid>/settings")
def settings(qid: str):
    db = get_db()
    q = _product_or_404(db, qid)
    direct, via_category = _schedules_for(db, q)
    vendor_ids = sorted(
        {s.vendor_id for s in direct}
        | {vid for (vid,) in db.execute(
            select(PriceObservation.vendor_id).where(PriceObservation.query_object_id == q.id).distinct()
        ).all()}
    )
    return render_template(
        "product_settings.html",
        band_rows=bands_by_vendor(db, q, vendor_ids),
        **_frame(db, q, "settings"),
        categories=db.scalars(select(Category).order_by(Category.id)).all(),
        stats=_query_price_stats(db, q.id),
        schedules=direct,
        category_schedules=via_category,
    )
