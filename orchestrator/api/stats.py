"""Dashboard summary: queue depth + recent job throughput."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from croniter import croniter
from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import func, select

from orchestrator import get_db
from orchestrator.models import Job, Node
from orchestrator.queue import read_deadletter, stream_stats

bp = Blueprint("stats", __name__, url_prefix="/api/v1")


@bp.get("/stats")
def stats():
    settings = current_app.config["SETTINGS"]
    db = get_db()

    try:
        queue = stream_stats(settings=settings)
        queue_ok = True
    except Exception as exc:  # noqa: BLE001 - Redis down shouldn't 500 the dashboard
        queue = {"error": str(exc), "depth": 0, "pending": 0, "consumers": 0, "deadletter": 0}
        queue_ok = False

    since = datetime.now(timezone.utc) - timedelta(hours=24)
    rows = db.execute(
        select(Job.status, func.count())
        .where(Job.created_at >= since)
        .group_by(Job.status)
    ).all()
    by_status = {status: count for status, count in rows}

    nodes = db.scalars(select(Node)).all()

    return jsonify(
        {
            "queue": queue,
            "queue_ok": queue_ok,
            "jobs_24h": {
                "total": sum(by_status.values()),
                "by_status": by_status,
            },
            "nodes": {
                "count": len(nodes),
                "replicas_running": sum(n.replicas_running for n in nodes),
                "replicas_desired": sum(n.replicas_desired for n in nodes),
            },
        }
    )


@bp.get("/cron/preview")
def cron_preview():
    """Validate a cron expression and return its next few UTC fire times."""
    expr = (request.args.get("expr") or "").strip()
    n = min(int(request.args.get("n", 5)), 20)
    if not expr or not croniter.is_valid(expr):
        return jsonify({"valid": False, "next": []})
    base = datetime.now(timezone.utc)
    it = croniter(expr, base)
    return jsonify(
        {"valid": True, "next": [it.get_next(datetime).isoformat() for _ in range(n)]}
    )


@bp.get("/deadletter")
def deadletter():
    settings = current_app.config["SETTINGS"]
    try:
        return jsonify(read_deadletter(100, settings=settings))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502
