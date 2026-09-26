"""Dashboard routes. Display pages query the ORM directly; mutating forms reuse
the same helpers the JSON API uses, then redirect back.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from sqlalchemy import func, or_, select

from orchestrator import get_db
from orchestrator.models import (
    Anomaly,
    Category,
    CategorySchedule,
    DroppedListing,
    Job,
    Listing,
    ListingPricePoint,
    Node,
    PriceObservation,
    QueryObject,
    Schedule,
    TrackedListing,
    Vendor,
)
from orchestrator.prices import delete_tracked_listing, recompute_for_tracked
from orchestrator.provisioning import NodeSpec, ProvisioningError, get_provisioner
from orchestrator.queue import delete_message, purge_queue, read_deadletter, stream_stats
from orchestrator.schemas import QueryObject as QuerySchema
from orchestrator.schemas import VendorConfig
from orchestrator.timeutil import iso_utc

bp = Blueprint(
    "ui",
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/ui-static",
)

SELECTOR_TYPES = ("text", "link", "image", "price", "additive_price", "misc_price")
JOB_SORTS = {
    "created_desc": Job.created_at.desc(),
    "created_asc": Job.created_at.asc(),
    "status": Job.status.asc(),
    "vendor": Job.vendor_id.asc(),
    "query": Job.query_object_id.asc(),
    "duration_desc": Job.duration_ms.desc().nullslast(),
}


def _settings():
    return current_app.config["SETTINGS"]


def _queue_stats() -> dict:
    try:
        return stream_stats(settings=_settings())
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "depth": 0, "pending": 0, "consumers": 0, "deadletter": 0}


def _vendor_map(db) -> dict[str, Vendor]:
    return {v.identifier: v for v in db.scalars(select(Vendor)).all()}


def _query_map(db) -> dict[str, QueryObject]:
    return {q.id: q for q in db.scalars(select(QueryObject)).all()}


def _jobs_query(db, args):
    stmt = select(Job)
    if s := args.get("status"):
        stmt = stmt.where(Job.status == s)
    if v := args.get("vendor_id"):
        stmt = stmt.where(Job.vendor_id == v)
    if q := args.get("query_object_id"):
        stmt = stmt.where(Job.query_object_id == q)
    if term := (args.get("q") or "").strip():
        # Job has no FK to QueryObject, so resolve matching ids first (by
        # display name or the id itself) then filter on those.
        like = f"%{term}%"
        matching = db.scalars(
            select(QueryObject.id).where(
                or_(QueryObject.content.ilike(like), QueryObject.id.ilike(like))
            )
        ).all()
        stmt = stmt.where(Job.query_object_id.in_(matching or ["__none__"]))
    order = JOB_SORTS.get(args.get("sort", "created_desc"), JOB_SORTS["created_desc"])
    limit = min(int(args.get("limit", 100)), 500)
    return db.scalars(stmt.order_by(order).limit(limit)).all()


def _reltime(dt) -> str:
    """'in 14m' / '2m ago' / '—' — relative to now, for schedules and freshness."""
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = (dt - datetime.now(timezone.utc)).total_seconds()
    future = secs > 0
    secs = abs(secs)
    if secs < 90:
        out = "now" if secs < 20 else f"{int(secs)}s"
    elif secs < 3600:
        out = f"{int(secs // 60)}m"
    elif secs < 86400:
        h = int(secs // 3600)
        m = int((secs % 3600) // 60)
        out = f"{h}h {m}m" if m else f"{h}h"
    else:
        out = f"{int(secs // 86400)}d"
    if out == "now":
        return "now"
    return f"in {out}" if future else f"{out} ago"


@bp.app_template_filter("reltime")
def _reltime_filter(dt):
    return _reltime(dt)


@bp.app_context_processor
def _nav_context():
    """Sidebar system-status figures. Never raises — a dead Redis or empty DB
    just shows zeros."""
    nav = {"queue_depth": 0, "workers_up": 0, "workers_desired": 0,
           "deadletter": 0, "freshness": "—", "redis_ok": True, "anomalies": 0}
    try:
        q = _queue_stats()
        nav["queue_depth"] = q.get("depth", 0)
        nav["deadletter"] = q.get("deadletter", 0)
        nav["redis_ok"] = "error" not in q
    except Exception:  # noqa: BLE001
        nav["redis_ok"] = False
    try:
        db = get_db()
        for n in db.scalars(select(Node)).all():
            nav["workers_up"] += n.replicas_running or 0
            nav["workers_desired"] += n.replicas_desired or 0
        last = db.scalar(select(func.max(Job.result_at)))
        nav["freshness"] = _reltime(last) if last else "—"
        nav["anomalies"] = db.scalar(
            select(func.count()).select_from(Anomaly).where(Anomaly.status == "open")
        ) or 0
    except Exception:  # noqa: BLE001
        pass
    return {"nav": nav, "provider": _settings().provider}


# ---- dashboard ----


def _sparkline(db, qid: str, points: int = 14) -> list[float]:
    """Last N median-price observations for a query object, oldest-first."""
    rows = db.scalars(
        select(PriceObservation.price_median)
        .where(PriceObservation.query_object_id == qid, PriceObservation.price_median.is_not(None))
        .order_by(PriceObservation.observed_at.desc())
        .limit(points)
    ).all()
    return [round(r, 2) for r in reversed(rows)]


def _vitals_context(db) -> dict:
    """Live figures for the Overview vital-signs band + sidebar. Cheap enough to
    poll on a short interval at panel scale."""
    queue = _queue_stats()
    nodes = db.scalars(select(Node)).all()
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    jobs_24h = db.scalar(select(func.count()).select_from(Job).where(Job.created_at >= since)) or 0
    by_status = _job_counts_since(db, since)
    # zero-count statuses are dropped in the template
    jobs_split = [
        {"status": s, "count": by_status.get(s, 0), "level": lvl}
        for s, lvl in (
            ("ok", "ok"), ("partial", "warn"), ("error", "crit"), ("dead", "crit"),
            ("queued", None), ("running", None), ("canceled", None),
        )
    ]
    obs_total = db.scalar(select(func.count()).select_from(PriceObservation)) or 0
    first_q = db.scalar(select(QueryObject.id).order_by(QueryObject.id).limit(1))
    return {
        "queue": queue,
        "queue_depth": queue.get("depth", 0),
        "queue_pending": queue.get("pending", 0),
        "workers_up": sum(n.replicas_running or 0 for n in nodes),
        "workers_desired": sum(n.replicas_desired or 0 for n in nodes),
        "jobs_24h": jobs_24h,
        "jobs_split": jobs_split,
        "obs_total": obs_total,
        "obs_spark": _sparkline(db, first_q) if first_q else [],
        "freshness": db.scalar(select(func.max(Job.result_at))),
        "consumers": queue.get("consumers", 0),
        "deadletter": queue.get("deadletter", 0),
        "redis_error": queue.get("error"),
    }


def _attention(db, queue: dict, nodes) -> list[dict]:
    """Things an operator should look at now — semantic, most-severe first."""
    items: list[dict] = []
    if "error" in queue:
        items.append({"level": "crit", "text": f"Redis unreachable — {queue.get('error', 'no connection')}"})
    if queue.get("deadletter"):
        items.append({"level": "crit",
                      "text": f"{queue['deadletter']} run(s) in the dead-letter stream",
                      "href": "runs", "args": {"status": "dead"}})
    for n in nodes:
        if (n.replicas_running or 0) < (n.replicas_desired or 0):
            items.append({"level": "warn",
                          "text": f"{n.id} — {n.replicas_running}/{n.replicas_desired} replicas up",
                          "href": "nodes"})
    for q in db.scalars(select(QueryObject)).all():
        latest = db.scalars(
            select(PriceObservation)
            .where(PriceObservation.query_object_id == q.id)
            .order_by(PriceObservation.observed_at.desc())
            .limit(2)
        ).all()
        if latest and all((o.listings_kept or 0) == 0 for o in latest):
            items.append({"level": "warn",
                          "text": f"{q.content or q.id} — last {len(latest)} run(s) returned 0 kept listings",
                          "href": "vendors"})
    return items


@bp.get("/")
def dashboard():
    db = get_db()
    vitals = _vitals_context(db)
    nodes = db.scalars(select(Node).order_by(Node.id)).all()
    queries = db.scalars(select(QueryObject).order_by(QueryObject.id)).all()
    categories = db.scalars(select(Category).order_by(Category.id)).all()
    upcoming = db.scalars(
        select(Schedule).where(Schedule.enabled).order_by(Schedule.next_run_at.asc().nullslast()).limit(5)
    ).all()
    return render_template(
        "dashboard.html",
        vitals=vitals,
        nodes=nodes,
        jobs=_jobs_query(db, {"limit": 8}),
        upcoming=upcoming,
        attention=_attention(db, vitals["queue"], nodes),
        queries=queries,
        categories=categories,
        vmap=_vendor_map(db),
        qmap=_query_map(db),
        qo_names={q.id: (q.content or q.id) for q in queries},
        qo_options=sorted(
            (
                {
                    "id": q.id,
                    "name": q.content or q.id,
                    "category": q.category.name or q.category.id if q.category else "Uncategorised",
                }
                for q in queries
            ),
            key=lambda o: (o["category"] == "Uncategorised", o["category"].lower(), o["name"].lower()),
        ),
        jobs_per_min=_jobs_per_min_by_node(db),
    )


def _job_counts(db) -> dict:
    rows = db.execute(select(Job.status, func.count()).group_by(Job.status)).all()
    return {s: c for s, c in rows}


def _job_counts_since(db, since) -> dict:
    rows = db.execute(
        select(Job.status, func.count()).where(Job.created_at >= since).group_by(Job.status)
    ).all()
    return {s: c for s, c in rows}


_JOBS_PER_MIN_WINDOW_MIN = 5


def _jobs_per_min_by_node(db) -> dict[str, float]:
    """Completed jobs per minute per node, over a short trailing window --
    recent enough to reflect current throughput rather than an all-time average."""
    since = datetime.now(timezone.utc) - timedelta(minutes=_JOBS_PER_MIN_WINDOW_MIN)
    rows = db.execute(
        select(Job.node_id, func.count())
        .where(Job.node_id.is_not(None), Job.result_at >= since)
        .group_by(Job.node_id)
    ).all()
    return {node_id: round(count / _JOBS_PER_MIN_WINDOW_MIN, 1) for node_id, count in rows}


@bp.get("/partials/queue")
def partial_queue():
    return render_template("partials/queue.html", vitals=_vitals_context(get_db()))


@bp.get("/partials/job-feed")
def partial_job_feed():
    db = get_db()
    return render_template(
        "partials/job_feed.html",
        jobs=_jobs_query(db, {"limit": 8}),
        vmap=_vendor_map(db),
        qmap=_query_map(db),
    )


@bp.get("/partials/nodes")
def partial_nodes():
    nodes = get_db().scalars(select(Node).order_by(Node.id)).all()
    return render_template("partials/nodes.html", nodes=nodes)


@bp.get("/partials/node-cards")
def partial_node_cards():
    db = get_db()
    nodes = db.scalars(select(Node).order_by(Node.id)).all()
    return render_template(
        "partials/node_cards.html", nodes=nodes, jobs_per_min=_jobs_per_min_by_node(db)
    )


@bp.get("/partials/jobs")
def partial_jobs():
    db = get_db()
    return render_template(
        "partials/job_table.html",
        jobs=_jobs_query(db, request.args),
        vmap=_vendor_map(db),
        qmap=_query_map(db),
    )


# ---- vendors ----


def _lines(name: str) -> list[str]:
    return [x.strip() for x in (request.form.get(name) or "").splitlines() if x.strip()]


def _num(name: str):
    val = request.form.get(name)
    return float(val) if val not in (None, "") else None


def _config_from_form() -> dict:
    """Build a frogscraper vendor config from the structured form, or the raw JSON
    override if the operator pasted one."""
    raw = (request.form.get("config_json") or "").strip()
    if raw:
        return json.loads(raw)

    reqd = set(request.form.getlist("sel_required"))
    ign = set(request.form.getlist("sel_ignore"))
    selectors: dict = {"listings": (request.form.get("listings") or "").strip()}
    for nm, css, typ in zip(
        request.form.getlist("sel_name"),
        request.form.getlist("sel_css"),
        request.form.getlist("sel_type"),
        strict=False,
    ):
        nm, css = nm.strip(), css.strip()
        if not nm or not css:
            continue
        d: dict = {"css_selector": css, "type": typ or "text"}
        if nm in reqd:
            d["required"] = True
        if nm in ign:
            d["ignore_filter"] = True
        selectors[nm] = d

    meta = {
        k: v
        for k, v in (
            ("name", request.form.get("name")),
            ("homepage", request.form.get("homepage")),
            ("color", request.form.get("color")),
        )
        if v
    }
    config: dict = {
        "metadata": meta,
        "url": {"start": (request.form.get("url_start") or "").strip(),
                "end": (request.form.get("url_end") or "").strip()},
        "selectors": selectors,
    }
    if request.form.get("preload"):
        config["preload"] = request.form["preload"].strip()
    if _lines("strip_phrases"):
        config["strip_phrases"] = _lines("strip_phrases")
    if _lines("challenge_markers"):
        config["challenge_markers"] = _lines("challenge_markers")
    return config


@bp.get("/vendors")
def vendors():
    rows = get_db().scalars(select(Vendor).order_by(Vendor.identifier)).all()
    return render_template("vendors.html", vendors=rows, selector_types=SELECTOR_TYPES)


@bp.post("/vendors")
def create_vendor():
    db = get_db()
    identifier = (request.form.get("identifier") or "").strip()
    if not identifier:
        flash("identifier is required", "error")
        return redirect(url_for("ui.vendors"))
    if db.get(Vendor, identifier):
        flash(f"vendor '{identifier}' already exists", "error")
        return redirect(url_for("ui.vendors"))
    try:
        config = _config_from_form()
        VendorConfig.model_validate({"identifier": identifier, **config})
    except (json.JSONDecodeError, ValueError) as exc:
        flash(f"invalid vendor config: {exc}", "error")
        return redirect(url_for("ui.vendors"))
    db.add(
        Vendor(
            identifier=identifier,
            name=request.form.get("name") or config.get("metadata", {}).get("name", identifier),
            enabled=request.form.get("enabled") == "on",
            config=config,
        )
    )
    db.commit()
    flash(f"vendor '{identifier}' created", "ok")
    return redirect(url_for("ui.vendors"))


@bp.post("/vendors/<identifier>/update")
def update_vendor(identifier: str):
    db = get_db()
    v = db.get(Vendor, identifier)
    if v is None:
        flash("vendor not found", "error")
        return redirect(url_for("ui.vendors"))
    try:
        config = _config_from_form()
        VendorConfig.model_validate({"identifier": identifier, **config})
    except (json.JSONDecodeError, ValueError) as exc:
        flash(f"invalid vendor config: {exc}", "error")
        return redirect(url_for("ui.vendors"))
    v.config = config
    v.name = request.form.get("name") or v.name
    v.enabled = request.form.get("enabled") == "on"
    db.commit()
    flash(f"vendor '{identifier}' updated", "ok")
    return redirect(url_for("ui.vendors"))


@bp.post("/vendors/<identifier>/delete")
def delete_vendor(identifier: str):
    db = get_db()
    v = db.get(Vendor, identifier)
    if v:
        db.delete(v)
        db.commit()
        flash(f"vendor '{identifier}' deleted", "ok")
    return redirect(url_for("ui.vendors"))


# ---- categories ----


@bp.get("/categories")
def categories():
    rows = get_db().scalars(select(Category).order_by(Category.id)).all()
    return render_template("categories.html", categories=rows)


@bp.post("/categories")
def create_category():
    db = get_db()
    cid = (request.form.get("id") or "").strip()
    if not cid:
        flash("id is required", "error")
        return redirect(url_for("ui.categories"))
    if db.get(Category, cid):
        flash(f"category '{cid}' already exists", "error")
        return redirect(url_for("ui.categories"))
    c = Category(id=cid)
    _apply_category_form(c)
    db.add(c)
    db.commit()
    flash(f"category '{cid}' created", "ok")
    return redirect(url_for("ui.categories"))


def _apply_category_form(c: Category) -> None:
    c.name = request.form.get("name", c.name)
    c.extra_exclude = _lines("extra_exclude")
    c.notes = request.form.get("notes", c.notes)
    c.minimum_value_floor = _num("minimum_value_floor")
    c.maximum_value_ceiling = _num("maximum_value_ceiling")


@bp.post("/categories/<cid>/update")
def update_category(cid: str):
    db = get_db()
    c = db.get(Category, cid)
    if c is None:
        flash("category not found", "error")
        return redirect(url_for("ui.categories"))
    _apply_category_form(c)
    db.commit()
    flash(f"category '{cid}' updated", "ok")
    return redirect(url_for("ui.categories"))


@bp.post("/categories/<cid>/delete")
def delete_category(cid: str):
    db = get_db()
    c = db.get(Category, cid)
    if c:
        db.delete(c)
        db.commit()
        flash(f"category '{cid}' deleted", "ok")
    return redirect(url_for("ui.categories"))


@bp.post("/categories/<cid>/apply-excludes")
def apply_category_excludes(cid: str):
    from orchestrator.excludes import apply_excludes_to_category

    db = get_db()
    c = db.get(Category, cid)
    if c is None:
        flash("category not found", "error")
        return redirect(url_for("ui.categories"))
    n = apply_excludes_to_category(db, c)
    db.commit()
    flash(f"hid {n} existing listing(s) across {len(c.queries)} query object(s) in category '{cid}'", "ok")
    return redirect(url_for("ui.categories"))


# ---- query objects ----


def _query_price_stats(db, qid: str) -> dict | None:
    rows = db.scalars(
        select(PriceObservation)
        .where(PriceObservation.query_object_id == qid, PriceObservation.excluded.is_(False))
        .order_by(PriceObservation.observed_at.asc())
    ).all()
    if not rows:
        return None
    lows = [r.price_low for r in rows if r.price_low is not None]
    means = [r.price_mean for r in rows if r.price_mean is not None]
    latest = rows[-1]
    by_vendor: dict[str, dict] = {}
    for r in rows:
        b = by_vendor.setdefault(
            r.vendor_id, {"vendor_id": r.vendor_id, "n": 0, "mean_sum": 0.0, "last": None,
                          "last_median": None}
        )
        b["n"] += 1
        if r.price_mean is not None:
            b["mean_sum"] += r.price_mean
        b["last"] = r.observed_date
        b["last_median"] = r.price_median
    for b in by_vendor.values():
        b["mean_avg"] = round(b["mean_sum"] / b["n"], 2) if b["n"] else None
    return {
        "observations": len(rows),
        "first_date": rows[0].observed_date,
        "last_date": latest.observed_date,
        "low_min": round(min(lows), 2) if lows else None,
        "mean_avg": round(sum(means) / len(means), 2) if means else None,
        "mean_max": round(max(means), 2) if means else None,
        "latest": {
            "observed_date": latest.observed_date,
            "price_low": latest.price_low,
            "price_mean": latest.price_mean,
            "price_median": latest.price_median,
            "listings_kept": latest.listings_kept,
        },
        "by_vendor": sorted(by_vendor.values(), key=lambda x: x["vendor_id"]),
    }


def _active_listing_counts(db) -> dict[str, int]:
    """Number of currently-active (not ended/sold), non-hidden tracked listings
    per query object -- one bulk pass over every (vendor, query) run history
    instead of the per-row query :func:`_run_times_by_vendor` would need."""
    rows = db.execute(
        select(TrackedListing.query_object_id, TrackedListing.vendor_id, TrackedListing.last_seen_at)
        .where(~TrackedListing.hidden)
    ).all()
    run_times = _all_run_times_by_key(db)
    counts: dict[str, int] = {}
    for qid, vid, last_seen in rows:
        seen = _as_utc(last_seen)
        newer = [r for r in run_times.get((vid, qid), []) if seen is None or r > seen]
        if len(newer) < ENDED_AFTER_MISSED_RUNS:
            counts[qid] = counts.get(qid, 0) + 1
    return counts


@bp.get("/queries")
def queries():
    """Old Query objects page -- now the Products list."""
    return redirect(url_for("products.products_list"), code=301)


def _apply_query_form(q: QueryObject) -> None:
    q.content = request.form.get("content", q.content)
    q.include = _lines("include")
    q.exclude = _lines("exclude")
    q.minimum_value = _num("minimum_value") or 0.0
    q.maximum_value = _num("maximum_value")
    q.category_id = request.form.get("category_id") or None


@bp.post("/queries")
def create_query():
    db = get_db()
    qid = (request.form.get("id") or "").strip()
    if not qid:
        flash("id is required", "error")
        return redirect(url_for("products.products_list"))
    if db.get(QueryObject, qid):
        flash(f"query object '{qid}' already exists", "error")
        return redirect(url_for("products.products_list"))
    q = QueryObject(id=qid)
    _apply_query_form(q)
    try:
        QuerySchema.model_validate(q.snapshot())
    except ValueError as exc:
        flash(f"invalid query object: {exc}", "error")
        return redirect(url_for("products.products_list"))
    db.add(q)
    db.commit()
    flash(f"product '{qid}' created -- add a schedule to start collecting prices", "ok")
    return redirect(url_for("products.settings", qid=qid))


@bp.post("/queries/<qid>/update")
def update_query(qid: str):
    db = get_db()
    q = db.get(QueryObject, qid)
    if q is None:
        flash("product not found", "error")
        return redirect(url_for("products.products_list"))
    _apply_query_form(q)
    try:
        QuerySchema.model_validate(q.snapshot())
    except ValueError as exc:
        flash(f"invalid query object: {exc}", "error")
        return redirect(url_for("products.settings", qid=qid))
    db.commit()
    flash("changes saved", "ok")
    return redirect(url_for("products.settings", qid=qid))


@bp.post("/queries/<qid>/delete")
def delete_query(qid: str):
    db = get_db()
    q = db.get(QueryObject, qid)
    if q:
        n = len(q.schedules)
        db.delete(q)  # its schedules go with it (QueryObject.schedules cascade)
        db.commit()
        _reload_scheduler()
        flash(f"product '{qid}' deleted" + (f" along with its {n} schedule(s)" if n else ""), "ok")
    return redirect(url_for("products.products_list"))


@bp.post("/queries/<qid>/apply-excludes")
def apply_query_excludes(qid: str):
    from orchestrator.excludes import apply_excludes_to_query

    db = get_db()
    q = db.get(QueryObject, qid)
    if q is None:
        flash("product not found", "error")
        return redirect(url_for("products.products_list"))
    n = apply_excludes_to_query(db, q)
    db.commit()
    flash(f"hid {n} existing listing(s) for '{qid}' matching its exclude phrases", "ok")
    return redirect(url_for("products.settings", qid=qid))


# A listing absent from this many of its vendor's latest clean runs in a row
# is treated as sold or ended. One miss isn't enough: search results shuffle
# between runs, so a listing that's still for sale often drops off one page
# of results and comes back on the next run.
ENDED_AFTER_MISSED_RUNS = 2

TRACKED_LISTING_SORTS = {
    "last_seen": TrackedListing.last_seen_at.desc(),
    "first_seen": TrackedListing.first_seen_at.desc(),
    "price_asc": TrackedListing.last_price.asc(),
    "price_desc": TrackedListing.last_price.desc(),
}


def _as_utc(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _run_times_by_vendor(db, qid: str) -> dict[str, list]:
    """Sorted (desc) ``observed_at`` of every *trustworthy* scrape run per vendor
    for a query object — used to tell whether a listing was in the most recent
    run(s). A run with ``status == "error"`` (includes a blocked/challenge-page
    scrape that returned zero listings) proves nothing about what's still
    listed, so it is never held against a listing's absence. Neither is a run
    left out of trends (e.g. one that kept far fewer listings than usual)."""
    rows = db.execute(
        select(PriceObservation.vendor_id, PriceObservation.observed_at)
        .where(PriceObservation.query_object_id == qid, PriceObservation.status != "error",
               PriceObservation.excluded.is_(False))
        .order_by(PriceObservation.observed_at.desc())
    ).all()
    out: dict[str, list] = {}
    for vid, at in rows:
        out.setdefault(vid, []).append(_as_utc(at))
    return out


def _annotate_activity(rows, runs_by_vendor: dict[str, list]) -> None:
    """Attach ``.active`` / ``.runs_missed`` to each TrackedListing in place. A
    listing missing from its vendor's last ``ENDED_AFTER_MISSED_RUNS`` clean
    runs has probably sold or ended."""
    for t in rows:
        seen = _as_utc(t.last_seen_at)
        newer = [r for r in runs_by_vendor.get(t.vendor_id, []) if seen is None or r > seen]
        t.runs_missed = len(newer)
        t.active = t.runs_missed < ENDED_AFTER_MISSED_RUNS


@bp.get("/queries/<qid>/listings")
def query_listings(qid: str):
    """Old per-query listings page -- now the product's Listings tab."""
    return redirect(url_for("products.listings", qid=qid, **request.args), code=301)


@bp.post("/tracked/<int:tracked_id>/hide")
def hide_tracked(tracked_id: int):
    db = get_db()
    t = db.get(TrackedListing, tracked_id)
    if t:
        t.hidden = True
        recompute_for_tracked(db, [t.id])
        db.commit()
    return redirect(request.referrer or url_for("ui.tracked_listings"))


@bp.post("/tracked/<int:tracked_id>/unhide")
def unhide_tracked(tracked_id: int):
    db = get_db()
    t = db.get(TrackedListing, tracked_id)
    if t:
        t.hidden = False
        recompute_for_tracked(db, [t.id])
        db.commit()
    return redirect(request.referrer or url_for("ui.tracked_listings"))


@bp.post("/tracked/<int:tracked_id>/delete")
def delete_tracked(tracked_id: int):
    db = get_db()
    t = db.get(TrackedListing, tracked_id)
    if t:
        delete_tracked_listing(db, t)
        db.commit()
        flash("listing deleted, along with its price history, and taken out of the runs it was in", "ok")
    return redirect(request.referrer or url_for("ui.tracked_listings"))


# ---- tracked listings (price-over-time by canonical URL) ----

TRACKED_SORTS = {
    "last_seen": TrackedListing.last_seen_at.desc(),
    "first_seen": TrackedListing.first_seen_at.desc(),
    "price_asc": TrackedListing.last_price.asc(),
    "price_desc": TrackedListing.last_price.desc(),
}


def _all_run_times_by_key(db) -> dict[tuple[str, str], list]:
    """(vendor_id, query_object_id) -> descending list of *trustworthy* run
    ``observed_at`` — see :func:`_run_times_by_vendor` on why ``status ==
    "error"`` runs (including a blocked/challenge-page scrape) are excluded."""
    rows = db.execute(
        select(PriceObservation.vendor_id, PriceObservation.query_object_id,
               PriceObservation.observed_at)
        .where(PriceObservation.status != "error", PriceObservation.excluded.is_(False))
        .order_by(PriceObservation.observed_at.desc())
    ).all()
    out: dict[tuple[str, str], list] = {}
    for vid, qid, at in rows:
        out.setdefault((vid, qid), []).append(_as_utc(at))
    return out


@bp.get("/tracked")
def tracked_listings():
    db = get_db()
    stmt = select(TrackedListing)
    if vendor := request.args.get("vendor_id"):
        stmt = stmt.where(TrackedListing.vendor_id == vendor)
    if qo := request.args.get("query_object_id"):
        stmt = stmt.where(TrackedListing.query_object_id == qo)
    if lt := request.args.get("listing_type"):
        stmt = stmt.where(TrackedListing.listing_type == lt)
    show = request.args.get("show", "visible")
    if show == "visible":
        stmt = stmt.where(~TrackedListing.hidden)
    elif show == "hidden":
        stmt = stmt.where(TrackedListing.hidden)
    order = TRACKED_SORTS.get(request.args.get("sort", "last_seen"), TRACKED_SORTS["last_seen"])
    rows = list(db.scalars(stmt.order_by(order).limit(500)).all())

    runs = _all_run_times_by_key(db)
    for t in rows:
        seen = _as_utc(t.last_seen_at)
        newer = [r for r in runs.get((t.vendor_id, t.query_object_id), [])
                 if seen is None or r > seen]
        t.runs_missed = len(newer)
        t.active = t.runs_missed < ENDED_AFTER_MISSED_RUNS

    status = request.args.get("status", "all")
    if status == "active":
        rows = [t for t in rows if t.active]
    elif status == "inactive":
        rows = [t for t in rows if not t.active]

    return render_template(
        "tracked_listings.html",
        rows=rows,
        vmap=_vendor_map(db),
        qmap=_query_map(db),
        vendors=db.scalars(select(Vendor).order_by(Vendor.identifier)).all(),
        queries=db.scalars(select(QueryObject).order_by(QueryObject.id)).all(),
        args=request.args,
    )


@bp.get("/tracked/<int:tracked_id>")
def tracked_listing_detail(tracked_id: int):
    db = get_db()
    t = db.get(TrackedListing, tracked_id)
    if t is None:
        flash("tracked listing not found", "error")
        return redirect(url_for("ui.tracked_listings"))
    history = [{"t": iso_utc(p.observed_at), "price": p.price} for p in t.price_points]
    return render_template(
        "tracked_listing_detail.html", t=t, vmap=_vendor_map(db), qmap=_query_map(db), history=history
    )


# ---- schedules ----


@bp.get("/schedules")
def schedules():
    db = get_db()
    rows = db.scalars(select(Schedule).order_by(Schedule.id)).all()
    cat_rows = db.scalars(select(CategorySchedule).order_by(CategorySchedule.id)).all()
    return render_template(
        "schedules.html",
        schedules=rows,
        category_schedules=cat_rows,
        vendors=db.scalars(select(Vendor).order_by(Vendor.identifier)).all(),
        queries=db.scalars(select(QueryObject).order_by(QueryObject.id)).all(),
        categories=db.scalars(select(Category).order_by(Category.id)).all(),
    )


@bp.post("/schedules")
def create_schedule():
    from croniter import croniter

    db = get_db()
    cron = request.form.get("cron") or ""
    vendor_id = request.form.get("vendor_id")
    query_object_id = request.form.get("query_object_id")
    if not croniter.is_valid(cron):
        flash(f"invalid cron: {cron!r}", "error")
        return redirect(url_for("ui.schedules"))
    if not db.get(Vendor, vendor_id) or not db.get(QueryObject, query_object_id):
        flash("unknown vendor or query object", "error")
        return redirect(url_for("ui.schedules"))

    options = {}
    raw_opts = request.form.get("options")
    if raw_opts:
        try:
            options = json.loads(raw_opts)
        except json.JSONDecodeError as exc:
            flash(f"invalid options JSON: {exc}", "error")
            return redirect(url_for("ui.schedules"))

    db.add(
        Schedule(
            name=request.form.get("name", ""),
            vendor_id=vendor_id,
            query_object_id=query_object_id,
            cron=cron,
            options=options,
            enabled=request.form.get("enabled") == "on",
        )
    )
    db.commit()
    _reload_scheduler()
    flash("schedule created", "ok")
    return redirect(url_for("ui.schedules"))


@bp.post("/schedules/<int:sid>/delete")
def delete_schedule(sid: int):
    db = get_db()
    s = db.get(Schedule, sid)
    if s:
        db.delete(s)
        db.commit()
        _reload_scheduler()
        flash("schedule deleted", "ok")
    return redirect(url_for("ui.schedules"))


@bp.post("/schedules/<int:sid>/run")
def run_schedule(sid: int):
    db = get_db()
    s = db.get(Schedule, sid)
    if not s:
        flash("schedule not found", "error")
        return redirect(url_for("ui.schedules"))
    from orchestrator.scheduler import enqueue_for_schedule

    try:
        job_id, _ = enqueue_for_schedule(db, s, scheduled_by="panel:run-now")
        db.commit()
        flash(f"queued run {job_id[:8]}", "ok")
    except Exception as exc:  # noqa: BLE001
        flash(f"enqueue failed: {exc}", "error")
    return redirect(_local_next() or url_for("ui.schedules"))


def _local_next() -> str | None:
    """The form's ``next`` field when it is a path on this site, so a button
    can send the user back where they pressed it without opening a redirect
    to anywhere else."""
    nxt = request.form.get("next") or ""
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else None


def _reload_scheduler() -> None:
    try:
        from orchestrator.scheduler import reload_jobs

        reload_jobs()
    except Exception:  # noqa: BLE001
        pass


# ---- category schedules ----


@bp.post("/schedules/category")
def create_category_schedule():
    from croniter import croniter

    db = get_db()
    cron = request.form.get("cron") or ""
    category_id = request.form.get("category_id")
    vendor_id = request.form.get("vendor_id") or None
    if not croniter.is_valid(cron):
        flash(f"invalid cron: {cron!r}", "error")
        return redirect(url_for("ui.schedules"))
    if not db.get(Category, category_id):
        flash("unknown category", "error")
        return redirect(url_for("ui.schedules"))
    if vendor_id and not db.get(Vendor, vendor_id):
        flash("unknown vendor", "error")
        return redirect(url_for("ui.schedules"))

    options = {}
    raw_opts = request.form.get("options")
    if raw_opts:
        try:
            options = json.loads(raw_opts)
        except json.JSONDecodeError as exc:
            flash(f"invalid options JSON: {exc}", "error")
            return redirect(url_for("ui.schedules"))

    db.add(
        CategorySchedule(
            name=request.form.get("name", ""),
            category_id=category_id,
            vendor_id=vendor_id,
            cron=cron,
            options=options,
            enabled=request.form.get("enabled") == "on",
        )
    )
    db.commit()
    _reload_scheduler()
    flash("category schedule created", "ok")
    return redirect(url_for("ui.schedules"))


@bp.post("/schedules/category/<int:sid>/delete")
def delete_category_schedule(sid: int):
    db = get_db()
    cs = db.get(CategorySchedule, sid)
    if cs:
        db.delete(cs)
        db.commit()
        _reload_scheduler()
        flash("category schedule deleted", "ok")
    return redirect(url_for("ui.schedules"))


@bp.post("/schedules/category/<int:sid>/run")
def run_category_schedule(sid: int):
    db = get_db()
    cs = db.get(CategorySchedule, sid)
    if not cs:
        flash("category schedule not found", "error")
        return redirect(url_for("ui.schedules"))
    from orchestrator.scheduler import enqueue_for_category_schedule

    try:
        jobs = enqueue_for_category_schedule(db, cs, scheduled_by="panel:category-run-now")
        db.commit()
        n_vendors = 1 if cs.vendor_id else db.scalar(
            select(func.count()).select_from(Vendor).where(Vendor.enabled.is_(True))
        )
        flash(f"queued {len(jobs)} job(s) across {n_vendors} vendor(s)", "ok")
    except Exception as exc:  # noqa: BLE001
        flash(f"enqueue failed: {exc}", "error")
    return redirect(url_for("ui.schedules"))


# ---- runs (one scrape of one product at one vendor; a ``Job`` row) ----


@bp.get("/runs")
def runs():
    db = get_db()
    dead = []
    if request.args.get("status") == "dead":
        try:
            dead = read_deadletter(50, settings=_settings())
        except Exception:  # noqa: BLE001
            pass
    return render_template(
        "runs.html",
        jobs=_jobs_query(db, request.args),
        vendors=db.scalars(select(Vendor).order_by(Vendor.identifier)).all(),
        vmap=_vendor_map(db),
        qmap=_query_map(db),
        args=request.args,
        counts=_job_counts(db),
        deadletter=dead,
    )


@bp.get("/jobs")
def jobs():
    """Old Jobs page -- now Runs."""
    return redirect(url_for("ui.runs", **request.args), code=301)


def _drop_reason_group(reason: str) -> str:
    """A short label for why a listing wasn't kept, so a run's drops can be
    summarised ("9 excluded phrase · 3 below minimum price") instead of listed."""
    r = reason or ""
    if r.startswith("excluded_phrases_found"):
        return "excluded phrase"
    if r.startswith("phrases_not_found"):
        return "search phrase missing"
    if r.startswith("missing required field"):
        return "missing fields"
    if r.startswith("condition_for_parts"):
        return "for parts"
    if "not greater than MinimumValue" in r:
        return "below minimum price"
    if "MaximumValue" in r:
        return "above maximum price"
    return r.split(":")[0][:40] or "other"


def _previous_clean_runs(db, obs: PriceObservation, n: int) -> list[PriceObservation]:
    """Up to ``n`` runs of the same product at the same vendor just before this
    one, newest first, skipping errored runs and runs left out of trends --
    they prove nothing about what was listed."""
    return list(db.scalars(
        select(PriceObservation)
        .where(
            PriceObservation.vendor_id == obs.vendor_id,
            PriceObservation.query_object_id == obs.query_object_id,
            PriceObservation.status != "error",
            PriceObservation.excluded.is_(False),
            PriceObservation.observed_at < obs.observed_at,
        )
        .order_by(PriceObservation.observed_at.desc())
        .limit(n)
    ).all())


def _run_changes(db, job: Job, listings, obs: PriceObservation | None) -> dict:
    """What this run changed compared with the one before it: listings seen
    for the first time, listings whose price moved, and listings this run
    counts as gone -- missing for the ``ENDED_AFTER_MISSED_RUNS``-th clean run
    in a row, so each ended listing is reported once, by the run that ended it."""
    tracked_ids = {l.tracked_listing_id for l in listings if l.tracked_listing_id}
    points = db.scalars(
        select(ListingPricePoint)
        .where(ListingPricePoint.tracked_listing_id.in_(tracked_ids))
        .order_by(ListingPricePoint.observed_at.asc())
    ).all() if tracked_ids else []
    by_tracked: dict[int, list] = {}
    for p in points:
        by_tracked.setdefault(p.tracked_listing_id, []).append(p)

    new, moved = [], []
    for l in listings:
        history = by_tracked.get(l.tracked_listing_id, [])
        idx = next((i for i, p in enumerate(history) if p.job_id == job.job_id), None)
        if idx is None:
            continue
        if idx == 0:
            new.append({"listing": l, "price": l.price})
        elif history[idx - 1].price != l.price:
            before = history[idx - 1].price
            moved.append({"listing": l, "before": before, "price": l.price,
                          "delta": l.price - before,
                          "pct": (l.price - before) / before * 100 if before else None})
    moved.sort(key=lambda m: abs(m["pct"] or 0), reverse=True)

    def seen_in(run: PriceObservation) -> set[int]:
        return set(db.scalars(
            select(ListingPricePoint.tracked_listing_id).where(ListingPricePoint.job_id == run.job_id)
        ).all())

    ended, missing = [], 0
    earlier = (_previous_clean_runs(db, obs, ENDED_AFTER_MISSED_RUNS)
               if obs is not None and obs.status != "error" else [])
    if earlier:
        seen_since = set()  # listings seen in any run after the oldest one checked
        for run in earlier[:-1]:
            seen_since |= seen_in(run)
        missing = len(seen_in(earlier[0]) - tracked_ids)
        if len(earlier) == ENDED_AFTER_MISSED_RUNS:
            gone = seen_in(earlier[-1]) - seen_since - tracked_ids
            if gone:
                ended = db.scalars(
                    select(TrackedListing).where(TrackedListing.id.in_(gone))
                    .order_by(TrackedListing.last_price.asc())
                ).all()
    return {"new": new, "moved": moved, "ended": list(ended),
            "previous": earlier[0] if earlier else None,
            "missing": missing, "seen_before": len(tracked_ids) - len(new)}


@bp.get("/runs/<job_id>")
def run_detail(job_id: str):
    db = get_db()
    job = db.get(Job, job_id)
    if job is None:
        flash("run not found", "error")
        return redirect(url_for("ui.runs"))
    listings = db.scalars(
        select(Listing).where(Listing.job_id == job_id).order_by(Listing.price.asc())
    ).all()
    dropped = db.scalars(
        select(DroppedListing).where(DroppedListing.job_id == job_id)
    ).all()
    obs = db.scalars(
        select(PriceObservation)
        .where(PriceObservation.job_id == job_id)
        .order_by(PriceObservation.observed_at.desc())
    ).first()
    drop_groups: dict[str, int] = {}
    for d in dropped:
        g = _drop_reason_group(d.reason)
        drop_groups[g] = drop_groups.get(g, 0) + 1
    return render_template(
        "run_detail.html", job=job, listings=listings, dropped=dropped, obs=obs,
        drop_groups=sorted(drop_groups.items(), key=lambda kv: -kv[1]),
        changes=_run_changes(db, job, listings, obs),
        schedule=db.get(Schedule, job.schedule_id) if job.schedule_id else None,
        product=db.get(QueryObject, job.query_object_id),
        anomalies=db.scalars(select(Anomaly).where(Anomaly.job_id == job_id).order_by(Anomaly.id)).all(),
        vmap=_vendor_map(db), qmap=_query_map(db),
    )


@bp.post("/runs/<job_id>/trends")
def set_run_in_trends(job_id: str):
    """Leave a run out of price trends, or put it back."""
    db = get_db()
    obs = db.scalars(select(PriceObservation).where(PriceObservation.job_id == job_id)).first()
    if obs is None:
        flash("this run has no result to include or leave out", "error")
    else:
        obs.excluded = request.form.get("include") != "1"
        db.commit()
        flash("run left out of price trends" if obs.excluded else "run counted in price trends again", "ok")
    return redirect(url_for("ui.run_detail", job_id=job_id))


@bp.get("/jobs/<job_id>")
def job_detail(job_id: str):
    """Old Job detail page -- now the run page."""
    return redirect(url_for("ui.run_detail", job_id=job_id), code=301)


@bp.post("/jobs/<job_id>/cancel")
def cancel_job(job_id: str):
    db = get_db()
    j = db.get(Job, job_id)
    if j is None:
        flash("run not found", "error")
    elif j.status not in ("queued", "running"):
        flash(f"run is already {j.status}, nothing to cancel", "error")
    else:
        if j.msg_id:
            delete_message(j.msg_id, settings=_settings())
        j.status = "canceled"
        db.commit()
        flash(f"run {job_id[:8]} canceled and removed from the queue", "ok")
    return redirect(request.referrer or url_for("ui.runs"))


@bp.post("/queue/purge")
def purge_queue_view():
    db = get_db()
    removed = purge_queue(settings=_settings())
    n = 0
    for j in db.scalars(select(Job).where(Job.status.in_(("queued", "running")))).all():
        j.status = "canceled"
        n += 1
    db.commit()
    flash(f"purged {removed} queued message(s); marked {n} run(s) canceled", "ok")
    return redirect(request.referrer or url_for("ui.runs"))


# ---- nodes ----


@bp.get("/nodes")
def nodes():
    db = get_db()
    rows = db.scalars(select(Node).order_by(Node.id)).all()
    return render_template(
        "nodes.html",
        nodes=rows,
        settings=_settings(),
        worker_env=_settings().worker_base_env(),
        jobs_per_min=_jobs_per_min_by_node(db),
    )


@bp.post("/nodes")
def create_node():
    settings = _settings()
    spec = NodeSpec(
        image=request.form.get("image") or settings.worker_image,
        replicas=int(request.form.get("replicas") or 1),
        name=request.form.get("name") or None,
        cpu=_int_or_none(request.form.get("cpu")),
        memory_mb=_int_or_none(request.form.get("memory_mb")),
        gpu_class=request.form.get("gpu_class") or None,
    )
    try:
        provisioner = get_provisioner(settings)
        info = provisioner.create_node(spec)
        provisioner.close()
    except ProvisioningError as exc:
        flash(str(exc), "error")
        return redirect(url_for("ui.nodes"))

    _upsert_node_row(info)
    flash(f"node '{info.id}' created with {info.replicas_desired} replica(s)", "ok")
    return redirect(url_for("ui.nodes"))


@bp.post("/nodes/<node_id>/scale")
def scale_node(node_id: str):
    try:
        target = int(request.form.get("replicas"))
    except (TypeError, ValueError):
        flash("replicas must be a whole number", "error")
        return redirect(url_for("ui.nodes"))
    try:
        provisioner = get_provisioner(_settings())
        info = provisioner.scale(node_id, target)
        provisioner.close()
    except (ProvisioningError, ValueError) as exc:
        flash(str(exc), "error")
        return redirect(url_for("ui.nodes"))
    _upsert_node_row(info)
    flash(
        f"node '{node_id}' scaled to {info.replicas_desired} replica(s) "
        f"({info.replicas_running} running)",
        "ok",
    )
    return redirect(url_for("ui.nodes"))


@bp.post("/nodes/<node_id>/delete")
def delete_node(node_id: str):
    try:
        provisioner = get_provisioner(_settings())
        provisioner.delete_node(node_id)
        provisioner.close()
    except ProvisioningError as exc:
        flash(str(exc), "error")
        return redirect(url_for("ui.nodes"))
    db = get_db()
    row = db.get(Node, node_id)
    if row:
        db.delete(row)
        db.commit()
    flash(f"node '{node_id}' deleted and its worker containers stopped", "ok")
    return redirect(url_for("ui.nodes"))


def _int_or_none(v):
    try:
        return int(v) if v not in (None, "") else None
    except ValueError:
        return None


def _upsert_node_row(info) -> None:
    db = get_db()
    now = datetime.now(timezone.utc)
    row = db.get(Node, info.id) or Node(id=info.id, created_at=now)
    row.provider = info.provider
    row.status = info.status
    row.replicas_desired = info.replicas_desired
    row.replicas_running = info.replicas_running
    row.image = info.image or row.image
    row.meta = info.meta
    row.last_seen_at = now
    db.add(row)
    db.commit()


# ---- database ----


@bp.get("/database")
def database():
    from orchestrator import dbadmin

    settings = _settings()
    db = get_db()
    return render_template(
        "database.html",
        info=dbadmin.db_info(settings),
        counts=dbadmin.table_counts(db),
        backups=dbadmin.list_backups(settings),
        is_sqlite=dbadmin.sqlite_path(settings) is not None,
    )


@bp.post("/database/backup")
def create_backup_view():
    from orchestrator import dbadmin

    try:
        b = dbadmin.create_backup(_settings())
        flash(f"backup '{b.name}' created ({b.size_bytes // 1024} KB)", "ok")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("ui.database"))


@bp.get("/database/backup/<name>/download")
def download_backup_view(name: str):
    from orchestrator import dbadmin

    d = dbadmin.backups_dir(_settings())
    target = (d / name).resolve() if d else None
    if target is None or target.parent != d.resolve() or not target.exists():
        flash("backup not found", "error")
        return redirect(url_for("ui.database"))
    return send_file(target, as_attachment=True, download_name=name)


@bp.post("/database/backup/<name>/delete")
def delete_backup_view(name: str):
    from orchestrator import dbadmin

    if dbadmin.delete_backup(_settings(), name):
        flash(f"backup '{name}' deleted", "ok")
    else:
        flash("backup not found", "error")
    return redirect(url_for("ui.database"))


@bp.post("/database/vacuum")
def vacuum_view():
    from orchestrator import dbadmin

    try:
        dbadmin.vacuum(_settings())
        flash("database vacuumed", "ok")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("ui.database"))


@bp.post("/database/purge")
def purge_old_data_view():
    from orchestrator import dbadmin

    try:
        days = int(request.form.get("older_than_days", 90))
    except ValueError:
        flash("older_than_days must be a whole number", "error")
        return redirect(url_for("ui.database"))
    if days < 7:
        flash("refusing to purge anything newer than 7 days", "error")
        return redirect(url_for("ui.database"))
    db = get_db()
    result = dbadmin.purge_old(db, older_than_days=days)
    db.commit()
    flash(
        f"purged {result['jobs_deleted']} job(s) and {result['observations_deleted']} "
        f"observation(s) older than {days} days — tracked listings and their price "
        f"history were not touched",
        "ok",
    )
    return redirect(url_for("ui.database"))
