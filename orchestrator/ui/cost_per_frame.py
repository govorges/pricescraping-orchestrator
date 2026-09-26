"""Cost-per-frame: a vendor's latest scraped GPU prices divided by Tom's
Hardware's hierarchy fps, one bar per matched GPU query object.

Performance data comes from :mod:`orchestrator.benchmarks` (cached, re-fetched
once it is over 24 hours old); prices are the latest observation per query
object for the chosen vendor.
"""

from __future__ import annotations

import statistics

from flask import Blueprint, flash, redirect, render_template, request, url_for
from sqlalchemy import func, select

from orchestrator import get_db
from orchestrator.benchmarks import (
    BRANDS,
    MODES,
    RESOLUTIONS,
    get_gpu_benchmarks,
    gpu_brand,
    match_benchmark,
)
from orchestrator.models import PriceObservation, QueryObject, Vendor
from orchestrator.prices import recompute_observations
from orchestrator.ui.insights import _export
from orchestrator.ui.views import _as_utc

bp = Blueprint("cpf", __name__, template_folder="templates")

PRICE_FIELDS = {"median": "price_median", "low": "price_low"}


def _latest_prices(db, vendor_id: str, price_field: str) -> dict[str, tuple[float, object]]:
    """``{query_object_id: (price, observed_at)}`` from each query's most recent
    observation at this vendor that has a price."""
    col = getattr(PriceObservation, price_field)
    rows = db.execute(
        select(PriceObservation.query_object_id, col, PriceObservation.observed_at)
        .where(PriceObservation.vendor_id == vendor_id, col.is_not(None), PriceObservation.excluded.is_(False))
        .order_by(PriceObservation.observed_at.asc())
    ).all()
    return {qid: (price, _as_utc(at)) for qid, price, at in rows}


def _latest_run_times(db, vendor_id: str) -> dict[str, object]:
    """``{query_object_id: observed_at}`` of each query's newest run at this
    vendor, whatever its outcome -- to tell when the price shown comes from an
    older run because the newest one failed or had no usable price."""
    rows = db.execute(
        select(PriceObservation.query_object_id, func.max(PriceObservation.observed_at))
        .where(PriceObservation.vendor_id == vendor_id)
        .group_by(PriceObservation.query_object_id)
    ).all()
    return {qid: _as_utc(at) for qid, at in rows}


def _default_vendor(db, vendors: list[Vendor]) -> str | None:
    """The vendor with the most observations, so the page opens on a chart
    with something in it rather than the alphabetically-first empty one."""
    busiest = db.execute(
        select(PriceObservation.vendor_id, func.count())
        .group_by(PriceObservation.vendor_id)
        .order_by(func.count().desc())
    ).first()
    known = {v.identifier for v in vendors}
    if busiest and busiest[0] in known:
        return busiest[0]
    return vendors[0].identifier if vendors else None


def cost_per_frame_rows(db, bench_rows: list[dict], vendor_id: str, resolution: str,
                        price_field: str = "price_median") -> tuple[list[dict], list[QueryObject], list[dict]]:
    """Returns ``(rows, unmatched, unpriced)``:

    - ``rows``: priced + benchmarked GPUs, cheapest $/frame first;
    - ``unmatched``: query objects with a price here but no hierarchy entry;
    - ``unpriced``: hierarchy matches this vendor has no price for yet.
    """
    prices = _latest_prices(db, vendor_id, price_field)
    newest_runs = _latest_run_times(db, vendor_id)
    rows, unmatched, unpriced = [], [], []
    for q in db.scalars(select(QueryObject).order_by(QueryObject.id)).all():
        bench = match_benchmark(q.content or "", bench_rows)
        priced = prices.get(q.id)
        if bench is None:
            if priced:
                unmatched.append(q)
            continue
        fps = (bench.get("fps") or {}).get(resolution)
        if not fps:
            continue
        if not priced:
            unpriced.append({"query": q, "gpu": bench["name"]})
            continue
        price, at = priced
        msrp = bench.get("msrp")
        rows.append({
            "query_id": q.id,
            "query": q.content or q.id,
            "gpu": bench["name"],
            "brand": gpu_brand(bench["name"]) or gpu_brand(q.content or ""),
            "price": round(price, 2),
            "observed_at": at,
            # the newest run errored, was left out of trends, or had no price
            "from_older_run": bool(at and newest_runs.get(q.id) and newest_runs[q.id] > at),
            "fps": fps,
            "cost_per_frame": round(price / fps, 2),
            "msrp": msrp,
            "msrp_cost_per_frame": round(msrp / fps, 2) if msrp else None,
        })
    rows.sort(key=lambda r: r["cost_per_frame"])
    return rows, unmatched, unpriced


def _params():
    mode = request.args.get("mode", "raster")
    res = request.args.get("res", "1440p")
    price = request.args.get("price", "median")
    return (
        mode if mode in MODES else "raster",
        res if res in RESOLUTIONS else "1440p",
        price if price in PRICE_FIELDS else "median",
    )


@bp.get("/cost-per-frame")
def cost_per_frame():
    db = get_db()
    vendors = db.scalars(select(Vendor).order_by(Vendor.identifier)).all()
    vendor_id = request.args.get("vendor") or _default_vendor(db, vendors)
    if vendor_id and vendor_id not in {v.identifier for v in vendors}:
        vendor_id = _default_vendor(db, vendors)
    mode, res, price = _params()

    bench = get_gpu_benchmarks(db)
    rows, unmatched, unpriced = (
        cost_per_frame_rows(db, bench.data.get(mode, []), vendor_id, res, PRICE_FIELDS[price])
        if vendor_id else ([], [], [])
    )
    return render_template(
        "cost_per_frame.html",
        vendors=vendors, vendor_id=vendor_id, mode=mode, res=res, price=price,
        modes=MODES, resolutions=RESOLUTIONS, bench=bench,
        rows=rows, unmatched=unmatched, unpriced=unpriced,
        max_cpf=max((r["cost_per_frame"] for r in rows), default=0),
        max_fps=max((r["fps"] for r in rows), default=0),
        # legend lists only the brands actually on the chart, in a fixed order
        brands={k: v for k, v in BRANDS.items() if any(r["brand"] == k for r in rows)},
        median_cpf=statistics.median(r["cost_per_frame"] for r in rows) if rows else None,
    )


@bp.post("/cost-per-frame/refresh")
def refresh_benchmarks():
    bench = get_gpu_benchmarks(get_db(), force=True)
    if bench.error:
        flash(f"Couldn't refresh benchmark data: {bench.error}", "error")
    else:
        flash("Benchmark data refreshed from Tom's Hardware.", "ok")
    return redirect(request.form.get("next") or url_for("cpf.cost_per_frame"))


@bp.post("/cost-per-frame/recompute")
def recompute_prices():
    vendor_id = request.form.get("vendor") or ""
    db = get_db()
    changed = recompute_observations(db, vendor_id)
    db.commit()
    flash(f"Prices recalculated from current listings: {changed} run(s) updated.", "ok")
    return redirect(request.form.get("next") or url_for("cpf.cost_per_frame"))


_EXPORT_FIELDS = [
    "gpu", "brand", "query_id", "query", "price", "observed_at", "fps",
    "cost_per_frame", "msrp", "msrp_cost_per_frame",
]


@bp.get("/cost-per-frame/export.<fmt>")
def export_cost_per_frame(fmt: str):
    db = get_db()
    vendor_id = request.args.get("vendor") or ""
    mode, res, price = _params()
    bench = get_gpu_benchmarks(db)
    rows, _, _ = cost_per_frame_rows(db, bench.data.get(mode, []), vendor_id, res, PRICE_FIELDS[price])
    return _export(fmt, _EXPORT_FIELDS, rows, f"cost-per-frame-{vendor_id}-{mode}-{res}-{price}")
