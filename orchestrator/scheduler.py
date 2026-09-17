"""APScheduler loop: cron ticks that enqueue jobs + a periodic reconcile pass.

Run exactly one scheduler process (``python -m orchestrator --scheduler``). It
uses its own SQLAlchemy sessions (:func:`orchestrator.db.session_scope`), never a
Flask request session.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from croniter import croniter
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.autoscale import desired_replicas
from orchestrator.db import session_scope
from orchestrator.jobs import build_job
from orchestrator.logging import get_logger
from orchestrator.models import CategorySchedule, Job, Node, Schedule, Vendor
from orchestrator.queue import enqueue, read_deadletter, stream_stats
from orchestrator.settings import Settings, get_settings

log = get_logger("orchestrator.scheduler")

_scheduler: BackgroundScheduler | None = None
_settings: Settings | None = None
_lock = threading.Lock()

_RECONCILE_ID = "reconcile"


def _job_id_for(schedule_id: int) -> str:
    return f"schedule:{schedule_id}"


def _job_id_for_category(category_schedule_id: int) -> str:
    return f"catschedule:{category_schedule_id}"


# ---- job assembly shared by the cron tick and the API "run now" ----


def enqueue_for_schedule(db: Session, schedule: Schedule, *, scheduled_by: str) -> tuple[str, str]:
    """Build + enqueue one job for ``schedule`` and record a :class:`Job` row.

    The caller owns the transaction (``db.commit()``).
    """
    settings = _settings or get_settings()
    payload = build_job(
        schedule.vendor,
        schedule.query_object,
        options=schedule.options or {},
        scheduled_by=scheduled_by,
    )
    msg_id = enqueue(payload, settings=settings)

    db.add(
        Job(
            job_id=payload["job_id"],
            schedule_id=schedule.id,
            vendor_id=schedule.vendor_id,
            query_object_id=schedule.query_object_id,
            status="queued",
            attempt=1,
            scheduled_by=scheduled_by,
            msg_id=msg_id,
            created_at=datetime.now(timezone.utc),
        )
    )
    now = datetime.now(timezone.utc)
    schedule.last_run_at = now
    if croniter.is_valid(schedule.cron):
        schedule.next_run_at = croniter(schedule.cron, now).get_next(datetime)
    return payload["job_id"], msg_id


def _run_schedule(schedule_id: int) -> None:
    try:
        with session_scope() as db:
            schedule = db.get(Schedule, schedule_id)
            if schedule is None or not schedule.enabled:
                return
            job_id, msg_id = enqueue_for_schedule(db, schedule, scheduled_by="scheduler:cron")
            log.info("schedule %s enqueued job %s (%s)", schedule_id, job_id, msg_id)
    except Exception:
        log.exception("schedule %s tick failed", schedule_id)


def enqueue_for_category_schedule(
    db: Session, cs: CategorySchedule, *, scheduled_by: str
) -> list[tuple[str, str]]:
    """Fan out one job per (vendor, query object) pair for a category schedule.

    The caller owns the transaction (``db.commit()``).
    """
    settings = _settings or get_settings()
    vendors = (
        [cs.vendor] if cs.vendor_id
        else list(db.scalars(select(Vendor).where(Vendor.enabled.is_(True))).all())
    )
    queries = list(cs.category.queries or [])
    out: list[tuple[str, str]] = []
    now = datetime.now(timezone.utc)
    for vendor in vendors:
        for query in queries:
            payload = build_job(vendor, query, options=cs.options or {}, scheduled_by=scheduled_by)
            msg_id = enqueue(payload, settings=settings)
            db.add(
                Job(
                    job_id=payload["job_id"],
                    category_schedule_id=cs.id,
                    vendor_id=vendor.identifier,
                    query_object_id=query.id,
                    status="queued",
                    attempt=1,
                    scheduled_by=scheduled_by,
                    msg_id=msg_id,
                    created_at=now,
                )
            )
            out.append((payload["job_id"], msg_id))
    cs.last_run_at = now
    if croniter.is_valid(cs.cron):
        cs.next_run_at = croniter(cs.cron, now).get_next(datetime)
    return out


def _run_category_schedule(category_schedule_id: int) -> None:
    try:
        with session_scope() as db:
            cs = db.get(CategorySchedule, category_schedule_id)
            if cs is None or not cs.enabled:
                return
            jobs = enqueue_for_category_schedule(db, cs, scheduled_by="scheduler:cron")
            log.info("category schedule %s enqueued %d job(s)", category_schedule_id, len(jobs))
    except Exception:
        log.exception("category schedule %s tick failed", category_schedule_id)


# ---- reconcile pass ----


def _reconcile() -> None:
    settings = _settings or get_settings()
    try:
        _sweep_deadletter(settings)
    except Exception:
        log.exception("deadletter sweep failed")
    try:
        _reconcile_nodes(settings)
    except Exception:
        log.exception("node reconcile failed")


def _sweep_deadletter(settings: Settings) -> None:
    dead = read_deadletter(200, settings=settings)
    if not dead:
        return
    ids = [d["job_id"] for d in dead if d.get("job_id")]
    if not ids:
        return
    with session_scope() as db:
        for job in db.scalars(select(Job).where(Job.job_id.in_(ids))).all():
            if job.status not in ("ok", "partial", "error", "dead"):
                job.status = "dead"


def _reconcile_nodes(settings: Settings) -> None:
    from orchestrator.provisioning import ProvisioningError, get_provisioner

    try:
        provisioner = get_provisioner(settings)
    except ProvisioningError as exc:
        log.debug("no provisioner available: %s", exc)
        return

    try:
        nodes = provisioner.list_nodes()
    finally:
        provisioner.close()

    now = datetime.now(timezone.utc)
    with session_scope() as db:
        seen = set()
        for info in nodes:
            seen.add(info.id)
            row = db.get(Node, info.id) or Node(id=info.id)
            row.provider = info.provider
            row.status = info.status
            row.replicas_desired = info.replicas_desired
            row.replicas_running = info.replicas_running
            row.image = info.image or row.image
            row.meta = info.meta
            row.last_seen_at = now
            db.add(row)
        for row in db.scalars(select(Node)).all():
            if row.id not in seen and row.provider == settings.provider:
                row.status = "gone"
                row.replicas_running = 0

    if settings.autoscale_enabled:
        _autoscale(settings, nodes)


def _autoscale(settings: Settings, nodes: list) -> None:
    from orchestrator.provisioning import get_provisioner

    stats = stream_stats(settings=settings)
    want = desired_replicas(
        stats["depth"],
        stats["pending"],
        per_replica_target=settings.autoscale_per_replica_target,
        min_replicas=settings.autoscale_min_replicas,
        max_replicas=settings.autoscale_max_replicas,
    )
    if not nodes:
        log.info("autoscale: want %d replicas but no node exists to scale", want)
        return
    target = nodes[0]
    if target.replicas_desired == want:
        return
    log.info("autoscale: node %s %d -> %d replicas", target.id, target.replicas_desired, want)
    provisioner = get_provisioner(settings)
    try:
        provisioner.scale(target.id, want)
    finally:
        provisioner.close()


# ---- lifecycle ----


def start_scheduler(settings: Settings | None = None) -> BackgroundScheduler:
    global _scheduler, _settings
    with _lock:
        if _scheduler is not None:
            return _scheduler
        _settings = settings or get_settings()
        sched = BackgroundScheduler(timezone="UTC")
        sched.add_job(
            _reconcile,
            "interval",
            seconds=max(_settings.reconcile_interval_s, 5),
            id=_RECONCILE_ID,
            max_instances=1,
            coalesce=True,
            next_run_time=datetime.now(timezone.utc),
        )
        sched.start()
        _scheduler = sched
        reload_jobs()
        return sched


def get_scheduler() -> BackgroundScheduler | None:
    return _scheduler


def reload_jobs() -> None:
    """Sync live cron jobs to the enabled ``Schedule`` / ``CategorySchedule`` rows.
    No-op if not started."""
    if _scheduler is None:
        return
    with session_scope() as db:
        schedules = db.scalars(select(Schedule)).all()
        cat_schedules = db.scalars(select(CategorySchedule)).all()
        wanted: dict[str, tuple] = {}
        for s in schedules:
            if s.enabled and croniter.is_valid(s.cron):
                wanted[_job_id_for(s.id)] = (_run_schedule, s.id, s.cron)
        for cs in cat_schedules:
            if cs.enabled and croniter.is_valid(cs.cron):
                wanted[_job_id_for_category(cs.id)] = (_run_category_schedule, cs.id, cs.cron)

    existing = {j.id for j in _scheduler.get_jobs() if j.id != _RECONCILE_ID}

    for job_id in existing - set(wanted):
        _scheduler.remove_job(job_id)

    for job_id, (fn, sid, cron) in wanted.items():
        trigger = CronTrigger.from_crontab(cron, timezone="UTC")
        if job_id in existing:
            _scheduler.reschedule_job(job_id, trigger=trigger)
        else:
            _scheduler.add_job(
                fn,
                trigger=trigger,
                id=job_id,
                args=[sid],
                max_instances=1,
                coalesce=True,
            )
    log.info("scheduler synced: %d cron job(s)", len(wanted))


def shutdown() -> None:
    global _scheduler
    with _lock:
        if _scheduler is not None:
            _scheduler.shutdown(wait=False)
            _scheduler = None
