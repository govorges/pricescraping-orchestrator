"""CRUD for cron schedules + a synchronous "run now" trigger."""

from __future__ import annotations

from croniter import croniter
from flask import Blueprint, jsonify, request
from sqlalchemy import select

from orchestrator import get_db
from orchestrator.logging import get_logger
from orchestrator.models import Job, QueryObject, Schedule, Vendor

bp = Blueprint("schedules", __name__, url_prefix="/api/v1/schedules")
log = get_logger("orchestrator.api.schedules")


def schedule_dict(s: Schedule) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "vendor_id": s.vendor_id,
        "query_object_id": s.query_object_id,
        "cron": s.cron,
        "options": s.options,
        "enabled": s.enabled,
        "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
        "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None,
    }


def _sync_scheduler() -> None:
    """Best-effort: refresh the live APScheduler if one runs in this process."""
    try:
        from orchestrator.scheduler import reload_jobs

        reload_jobs()
    except Exception as exc:  # noqa: BLE001 - scheduler not running here is fine
        log.debug("scheduler not reloaded: %s", exc)


@bp.get("")
def list_schedules():
    db = get_db()
    rows = db.scalars(select(Schedule).order_by(Schedule.id)).all()
    return jsonify([schedule_dict(s) for s in rows])


@bp.post("")
def create_schedule():
    db = get_db()
    body = request.get_json(silent=True) or {}
    for key in ("vendor_id", "query_object_id", "cron"):
        if not body.get(key):
            return jsonify({"error": f"{key} is required"}), 400
    if not croniter.is_valid(body["cron"]):
        return jsonify({"error": f"invalid cron expression: {body['cron']!r}"}), 422
    if db.get(Vendor, body["vendor_id"]) is None:
        return jsonify({"error": f"unknown vendor '{body['vendor_id']}'"}), 422
    if db.get(QueryObject, body["query_object_id"]) is None:
        return jsonify({"error": f"unknown query object '{body['query_object_id']}'"}), 422

    s = Schedule(
        name=body.get("name", ""),
        vendor_id=body["vendor_id"],
        query_object_id=body["query_object_id"],
        cron=body["cron"],
        options=body.get("options") or {},
        enabled=bool(body.get("enabled", True)),
    )
    db.add(s)
    db.commit()
    _sync_scheduler()
    return jsonify(schedule_dict(s)), 201


@bp.get("/<int:sid>")
def get_schedule(sid: int):
    s = get_db().get(Schedule, sid)
    if s is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(schedule_dict(s))


@bp.patch("/<int:sid>")
@bp.put("/<int:sid>")
def update_schedule(sid: int):
    db = get_db()
    s = db.get(Schedule, sid)
    if s is None:
        return jsonify({"error": "not found"}), 404
    body = request.get_json(silent=True) or {}
    if "cron" in body:
        if not croniter.is_valid(body["cron"]):
            return jsonify({"error": f"invalid cron expression: {body['cron']!r}"}), 422
        s.cron = body["cron"]
    for key in ("name", "options", "enabled"):
        if key in body:
            setattr(s, key, body[key])
    db.commit()
    _sync_scheduler()
    return jsonify(schedule_dict(s))


@bp.delete("/<int:sid>")
def delete_schedule(sid: int):
    db = get_db()
    s = db.get(Schedule, sid)
    if s is None:
        return jsonify({"error": "not found"}), 404
    db.delete(s)
    db.commit()
    _sync_scheduler()
    return "", 204


@bp.post("/<int:sid>/run")
def run_schedule(sid: int):
    """Assemble + enqueue a job right now, independent of the cron loop."""
    db = get_db()
    s = db.get(Schedule, sid)
    if s is None:
        return jsonify({"error": "not found"}), 404

    from orchestrator.scheduler import enqueue_for_schedule

    try:
        job_id, msg_id = enqueue_for_schedule(db, s, scheduled_by="panel:run-now")
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"enqueue failed: {exc}"}), 502
    db.commit()
    return jsonify({"job_id": job_id, "msg_id": msg_id}), 202


@bp.get("/<int:sid>/jobs")
def schedule_jobs(sid: int):
    db = get_db()
    rows = db.scalars(
        select(Job).where(Job.schedule_id == sid).order_by(Job.created_at.desc()).limit(50)
    ).all()
    from orchestrator.api.jobs import job_dict

    return jsonify([job_dict(j) for j in rows])
