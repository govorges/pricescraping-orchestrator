"""Query-object analytics: the cross-query leaderboard and per-query stats
behind the Products pages (:mod:`orchestrator.ui.products`), and CSV/JSON
export of both. The old ``/insights`` pages now redirect to Products.
"""

from __future__ import annotations

import csv
import io
import json
import statistics
from datetime import datetime, timedelta, timezone

from flask import Blueprint, Response, abort, redirect, request, url_for
from sqlalchemy import func, select

from orchestrator import get_db
from orchestrator.models import Category, PriceObservation, QueryObject, TrackedListing
from orchestrator.timeutil import iso_utc
from orchestrator.ui.views import _active_listing_counts, _as_utc, _query_price_stats, _vendor_map

bp = Blueprint("insights", __name__, template_folder="templates")


# ---- shared data ----------------------------------------------------------


def _leaderboard(db, category_id: str | None = None) -> list[dict]:
    """One summary row per query object: latest price, 7-day change, reach.

    Two queries total regardless of how many query objects exist -- every
    observation needed is pulled in one pass and reduced in Python, rather
    than re-querying per query object.
    """
    stmt = select(QueryObject)
    if category_id:
        stmt = stmt.where(QueryObject.category_id == category_id)
    queries = db.scalars(stmt.order_by(QueryObject.id)).all()
    if not queries:
        return []
    qids = [q.id for q in queries]

    obs = db.execute(
        select(
            PriceObservation.query_object_id,
            PriceObservation.observed_at,
            PriceObservation.price_median,
            PriceObservation.vendor_id,
        )
        .where(PriceObservation.query_object_id.in_(qids), PriceObservation.price_median.is_not(None))
        .order_by(PriceObservation.observed_at.asc())
    ).all()
    by_qid: dict[str, list] = {}
    for qid, at, median, vendor in obs:
        by_qid.setdefault(qid, []).append((_as_utc(at), median, vendor))

    active_counts = _active_listing_counts(db)
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    rows = []
    for q in queries:
        series = by_qid.get(q.id, [])
        latest = series[-1][1] if series else None
        past = [m for at, m, _ in series if at <= cutoff]
        base = past[-1] if past else (series[0][1] if series else None)
        change = round((latest - base) / base * 100, 1) if latest is not None and base else None
        rows.append(
            {
                "id": q.id,
                "content": q.content or q.id,
                "category_id": q.category_id,
                "category": (q.category.name or q.category.id) if q.category else None,
                "latest_price": latest,
                "change_pct": change,
                "observations": len(series),
                "vendors": len({v for *_, v in series}),
                "active_listings": active_counts.get(q.id, 0),
                "spark": [m for _, m, _ in series[-14:]],
            }
        )
    return rows


_SORTS = {
    "content": lambda r: r["content"].lower(),
    "price": lambda r: r["latest_price"],
    "change": lambda r: r["change_pct"],
    "active": lambda r: r["active_listings"],
    "observations": lambda r: r["observations"],
}
# fields whose sort key can be None (missing data) -- those rows sort last
# regardless of ascending/descending, rather than jumping to the front when
# the direction reverses.
_NULLABLE_SORTS = {"price", "change"}


def _sort_leaderboard(rows: list[dict], sort: str) -> list[dict]:
    reverse = sort.endswith("_desc")
    base = sort.replace("_desc", "").replace("_asc", "")
    key_fn = _SORTS.get(base, _SORTS["content"])
    if base in _NULLABLE_SORTS:
        present = [r for r in rows if key_fn(r) is not None]
        missing = [r for r in rows if key_fn(r) is None]
        present.sort(key=key_fn, reverse=reverse)
        return present + missing
    return sorted(rows, key=key_fn, reverse=reverse)


def _listing_type_breakdown(db, qid: str) -> dict[str, int]:
    rows = db.execute(
        select(TrackedListing.listing_type, func.count())
        .where(TrackedListing.query_object_id == qid, ~TrackedListing.hidden)
        .group_by(TrackedListing.listing_type)
    ).all()
    return {t: c for t, c in rows}


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


def _query_insights(db, q: QueryObject) -> dict:
    stats = _query_price_stats(db, q.id) or {}
    history = db.scalars(
        select(PriceObservation)
        .where(PriceObservation.query_object_id == q.id)
        .order_by(PriceObservation.observed_at.asc())
    ).all()
    medians = [(_as_utc(r.observed_at), r.price_median) for r in history if r.price_median is not None]
    now = datetime.now(timezone.utc)

    def change_over(days: int) -> float | None:
        if not medians:
            return None
        cutoff = now - timedelta(days=days)
        past = [m for at, m in medians if at <= cutoff]
        base = past[-1] if past else medians[0][1]
        latest = medians[-1][1]
        return round((latest - base) / base * 100, 1) if base else None

    values = [m for _, m in medians]
    total_tracked = (
        db.scalar(
            select(func.count()).select_from(TrackedListing).where(TrackedListing.query_object_id == q.id)
        )
        or 0
    )

    return {
        "stats": stats,
        "history_json": [_obs_dict(o) for o in history],
        "history_rows": list(reversed(history))[:50],
        "change_7d": change_over(7),
        "change_30d": change_over(30),
        "volatility": round(statistics.pstdev(values), 2) if len(values) > 1 else None,
        "active_listings": _active_listing_counts(db).get(q.id, 0),
        "total_tracked": total_tracked,
        "listing_types": _listing_type_breakdown(db, q.id),
    }


# ---- pages ------------------------------------------------------------


@bp.get("/insights")
def insights_home():
    """Old leaderboard -- now the Products list."""
    category_id = request.args.get("category_id") or None
    return redirect(url_for("products.products_list", category_id=category_id), code=301)


@bp.get("/insights/<qid>")
def insight_detail(qid: str):
    """Old per-query deep-dive -- now the product's Overview tab."""
    return redirect(url_for("products.overview", qid=qid), code=301)


# ---- export -------------------------------------------------------------


def _csv_response(fieldnames: list[str], rows: list[dict], filename: str) -> Response:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _json_response(rows: list[dict], filename: str) -> Response:
    return Response(
        json.dumps(rows, indent=2, default=str),
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _export(fmt: str, fieldnames: list[str], rows: list[dict], stem: str) -> Response:
    if fmt not in ("csv", "json"):
        abort(404)
    name = f"{stem}.{fmt}"
    return _csv_response(fieldnames, rows, name) if fmt == "csv" else _json_response(rows, name)


_LEADERBOARD_FIELDS = [
    "id", "content", "category_id", "category", "latest_price",
    "change_pct", "observations", "vendors", "active_listings",
]
_OBS_FIELDS = [
    "observed_at", "observed_date", "vendor_id", "query_object_id", "job_id",
    "status", "price_low", "price_mean", "price_median", "listings_found", "listings_kept",
]


@bp.get("/insights/export.<fmt>")
def export_leaderboard(fmt: str):
    db = get_db()
    category_id = request.args.get("category_id") or None
    rows = [{k: r[k] for k in _LEADERBOARD_FIELDS} for r in _leaderboard(db, category_id)]
    return _export(fmt, _LEADERBOARD_FIELDS, rows, f"insights-{category_id or 'all'}")


@bp.get("/insights/<qid>/export.<fmt>")
def export_query_observations(qid: str, fmt: str):
    db = get_db()
    if db.get(QueryObject, qid) is None:
        abort(404)
    history = db.scalars(
        select(PriceObservation).where(PriceObservation.query_object_id == qid)
        .order_by(PriceObservation.observed_at.asc())
    ).all()
    return _export(fmt, _OBS_FIELDS, [_obs_dict(o) for o in history], f"observations-{qid}")


@bp.get("/insights/category/<cid>/export.<fmt>")
def export_category_observations(cid: str, fmt: str):
    db = get_db()
    cat = db.get(Category, cid)
    if cat is None:
        abort(404)
    qids = [q.id for q in cat.queries]
    history = (
        db.scalars(
            select(PriceObservation).where(PriceObservation.query_object_id.in_(qids))
            .order_by(PriceObservation.observed_at.asc())
        ).all()
        if qids
        else []
    )
    return _export(fmt, _OBS_FIELDS, [_obs_dict(o) for o in history], f"observations-{cid}")
