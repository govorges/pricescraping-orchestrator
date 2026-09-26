"""Anomalies inbox and Detection rules pages.

Detection itself lives in :mod:`orchestrator.anomalies` (it runs on ingest,
with no Flask involved); this module only lists, explains and acts on what it
raised, and edits the rules it reads.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from sqlalchemy import case, func, select

from orchestrator import get_db
from orchestrator.anomalies import (
    ACTIONS,
    AUCTION_MODES,
    KINDS,
    parse_rules_form,
    preview_counts,
    resolve,
    rules_for,
    save_rules,
)
from orchestrator.models import Anomaly, Category, Job, PriceObservation, QueryObject, Schedule
from orchestrator.ui.views import _local_next, _query_map, _vendor_map

bp = Blueprint("anomalies", __name__, template_folder="templates")

KIND_LABELS = {
    "outlier": "Outlier listings",
    "move": "Median moves",
    "cut": "Listing price cuts",
    "health": "Scrape health",
}
KIND_TAGS = {"outlier": "outlier listing", "move": "median move", "cut": "price cut", "health": "scrape health"}


def open_count(db) -> int:
    return db.scalar(select(func.count()).select_from(Anomaly).where(Anomaly.status == "open")) or 0


# ---- evidence charts (drawn server-side as SVG geometry) ------------------------


def _strip_chart(prices: list[float], flagged: float, median: float, threshold: float) -> dict:
    """Every listing in the run as a dot along a price axis, the flagged one
    highlighted, with the median and the rule's flag line marked."""
    top = max([*prices, flagged, median, threshold, 1.0]) * 1.08

    def x(p: float) -> float:
        return round(40 + p / top * 620, 1)

    others = list(prices)
    if flagged in others:
        others.remove(flagged)
    dots = [{"x": x(p), "y": round(46 + ((i * 37) % 23) / 22 * 104, 1)} for i, p in enumerate(others)]
    return {
        "dots": dots, "flag": {"x": x(flagged), "y": 98},
        "median": x(median), "threshold": x(threshold),
        "ticks": [{"x": x(top * f), "label": f"${top * f:,.0f}"} for f in (0, 0.25, 0.5, 0.75, 1)],
    }


def _bar_chart(values: list[int]) -> dict:
    """Listings kept per run, the flagged (last) run highlighted."""
    top = max([*values, 1]) * 1.15
    width = 620 / max(len(values), 1)
    avg = sum(values[:-1]) / max(len(values) - 1, 1)

    def y(v: float) -> float:
        return round(190 - v / top * 160, 1)

    return {
        # a zero-height bar would vanish; keep a sliver so "0" still reads as a bar
        "bars": [{"x": round(40 + i * width + width * 0.15, 1), "w": round(width * 0.7, 1), "y": min(y(v), 188),
                  "h": max(round(190 - y(v), 1), 2), "v": v, "flag": i == len(values) - 1}
                 for i, v in enumerate(values)],
        "avg_y": y(avg), "avg": round(avg),
    }


def _evidence(a: Anomaly) -> dict:
    d = a.details or {}
    if a.kind == "outlier" and d.get("run_prices"):
        return {"strip": _strip_chart(d["run_prices"], d["price"], d["median"], d["threshold"])}
    if a.kind == "health" and d.get("kept_series"):
        return {"bars": _bar_chart(d["kept_series"])}
    if a.kind == "move" and d.get("series"):
        return {"line": {"points": [p["median"] for p in d["series"]], "dates": [p["at"] for p in d["series"]],
                         "ref": d.get("baseline"), "ref_label": f"{d.get('window_days', 7)}-day median"}}
    if a.kind == "cut" and d.get("series"):
        return {"line": {"points": [p["price"] for p in d["series"]], "dates": [p["at"] for p in d["series"]],
                         "ref": d.get("median"), "ref_label": "run median"}}
    return {}


# ---- inbox ------------------------------------------------------------------------


@bp.get("/anomalies")
def inbox():
    db = get_db()
    status = request.args.get("status", "open")
    status = status if status in ("open", "reviewed") else "open"
    kind = request.args.get("kind", "")
    kind = kind if kind in KINDS else ""
    qid = request.args.get("qid") or ""
    job_id = request.args.get("job") or ""

    scoped = select(Anomaly)
    if qid:
        scoped = scoped.where(Anomaly.query_object_id == qid)
    if job_id:
        scoped = scoped.where(Anomaly.job_id == job_id)
    in_status = scoped.where(Anomaly.status == status)
    listed = in_status.where(Anomaly.kind == kind) if kind else in_status
    severity_rank = case({"high": 0, "med": 1}, value=Anomaly.severity, else_=2)
    rows = db.scalars(listed.order_by(severity_rank, Anomaly.detected_at.desc()).limit(200)).all()

    by_status = in_status.subquery()
    kind_counts = dict(db.execute(
        select(by_status.c.kind, func.count()).group_by(by_status.c.kind)
    ).all())
    all_scoped = scoped.subquery()
    status_counts = dict(db.execute(
        select(all_scoped.c.status, func.count()).group_by(all_scoped.c.status)
    ).all())

    selected = None
    if sel_id := request.args.get("id", type=int):
        selected = db.get(Anomaly, sel_id)
    if selected is None and rows:
        selected = rows[0]

    ctx = {}
    if selected is not None:
        job = db.get(Job, selected.job_id)
        ctx = {
            "evidence": _evidence(selected),
            "obs": db.scalars(select(PriceObservation).where(PriceObservation.job_id == selected.job_id)).first(),
            "schedule": db.get(Schedule, job.schedule_id) if job and job.schedule_id else None,
        }
    return render_template(
        "anomalies.html", rows=rows, selected=selected, status=status, kind=kind, qid=qid, job_id=job_id,
        kind_counts=kind_counts, status_counts=status_counts, kind_labels=KIND_LABELS, kind_tags=KIND_TAGS,
        qmap=_query_map(db), vmap=_vendor_map(db), **ctx,
    )


@bp.post("/anomalies/<int:aid>/resolve")
def resolve_anomaly(aid: int):
    db = get_db()
    a = db.get(Anomaly, aid)
    if a is None:
        abort(404)
    action = request.form.get("action", "")
    try:
        if action == "rerun":
            job = db.get(Job, a.job_id)
            schedule = db.get(Schedule, job.schedule_id) if job and job.schedule_id else None
            if schedule is None:
                raise ValueError("this run didn't come from a schedule, so it can't be re-run from here")
            from orchestrator.scheduler import enqueue_for_schedule

            enqueue_for_schedule(db, schedule, scheduled_by="panel:anomaly-rerun")
        label = resolve(db, a, action, phrase=request.form.get("phrase", ""))
        db.commit()
        flash(label, "ok")
    except Exception as exc:  # noqa: BLE001 -- surface it on the page, not as a 500
        db.rollback()
        flash(str(exc), "error")
    return redirect(_local_next() or url_for("anomalies.inbox", id=aid))


# ---- rules ------------------------------------------------------------------------


def _scope_category(db, scope: str) -> str | None:
    if scope == "default":
        return None
    if db.get(Category, scope) is None:
        abort(404)
    return scope


def _render_rules(db, scope: str, rules: dict, preview: dict | None):
    category_id = _scope_category(db, scope)
    since = datetime.now(timezone.utc) - timedelta(days=30)
    stmt = select(Anomaly.kind, func.count()).where(Anomaly.detected_at >= since)
    if category_id:
        qids = select(QueryObject.id).where(QueryObject.category_id == category_id)
        stmt = stmt.where(Anomaly.query_object_id.in_(qids))
    raised = dict(db.execute(stmt.group_by(Anomaly.kind)).all())
    return render_template(
        "anomaly_rules.html", scope=scope, rules=rules, preview=preview, raised=raised,
        categories=db.scalars(select(Category).order_by(Category.id)).all(),
        actions=ACTIONS, auction_modes=AUCTION_MODES, kind_labels=KIND_LABELS,
    )


@bp.get("/anomalies/rules")
def rules_page():
    db = get_db()
    scope = request.args.get("scope", "default")
    return _render_rules(db, scope, rules_for(db, _scope_category(db, scope)), None)


@bp.post("/anomalies/rules")
def save_rules_view():
    db = get_db()
    scope = request.form.get("scope", "default")
    base = rules_for(db, _scope_category(db, scope))
    rules = parse_rules_form(request.form, base)
    if request.form.get("intent") == "test":
        preview = preview_counts(db, rules, category_id=_scope_category(db, scope))
        db.rollback()  # detection reads only, but never keep anything a preview touched
        return _render_rules(db, scope, rules, preview)
    save_rules(db, scope, rules)
    db.commit()
    flash("Detection rules saved. They apply to runs from now on.", "ok")
    return redirect(url_for("anomalies.rules_page", scope=scope))
