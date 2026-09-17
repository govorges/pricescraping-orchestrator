"""Introspection + maintenance for the panel's Database page.

Row-count stats work against any dialect. Backup/vacuum are SQLite-specific
(the dev/default store); against Postgres/TimescaleDB in production those are
better handled by the managed database's own backup tooling, so the panel
just says so instead of pretending to support it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from orchestrator.models import (
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
from orchestrator.settings import Settings

# (label, model, "ephemeral" | "durable") — durable tables hold the persistent
# per-listing price history and are never touched by the retention purge.
TABLES: list[tuple[str, type, str]] = [
    ("vendor", Vendor, "config"),
    ("category", Category, "config"),
    ("query_object", QueryObject, "config"),
    ("schedule", Schedule, "config"),
    ("category_schedule", CategorySchedule, "config"),
    ("job", Job, "ephemeral"),
    ("price_observation", PriceObservation, "ephemeral"),
    ("listing", Listing, "ephemeral"),
    ("dropped_listing", DroppedListing, "ephemeral"),
    ("tracked_listing", TrackedListing, "durable"),
    ("listing_price_point", ListingPricePoint, "durable"),
    ("node", Node, "config"),
]


def table_counts(db: Session) -> list[dict]:
    return [
        {"table": name, "rows": db.scalar(select(func.count()).select_from(model)) or 0, "kind": kind}
        for name, model, kind in TABLES
    ]


def sqlite_path(settings: Settings) -> Path | None:
    """The on-disk path for a ``sqlite:///...`` URL, or ``None`` for any other dialect."""
    url = settings.database_url
    if not url.startswith("sqlite"):
        return None
    if url.startswith("sqlite:////"):  # absolute: sqlite:////abs/path.db
        return Path("/" + url[len("sqlite:////") :])
    return Path(url[len("sqlite:///") :])  # relative: sqlite:///./_local/orchestrator.db


def db_info(settings: Settings) -> dict:
    path = sqlite_path(settings)
    dialect = settings.database_url.split("://", 1)[0].split("+", 1)[0]
    info = {"dialect": dialect, "url": settings.database_url, "path": str(path) if path else None,
            "size_bytes": None}
    if path and path.exists():
        info["size_bytes"] = path.stat().st_size
    return info


def backups_dir(settings: Settings) -> Path | None:
    path = sqlite_path(settings)
    if path is None:
        return None
    d = path.parent / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class BackupFile:
    name: str
    size_bytes: int
    created_at: datetime


def list_backups(settings: Settings) -> list[BackupFile]:
    d = backups_dir(settings)
    if d is None:
        return []
    out = [
        BackupFile(
            name=p.name, size_bytes=p.stat().st_size,
            created_at=datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc),
        )
        for p in d.glob("*.db")
    ]
    return sorted(out, key=lambda b: b.created_at, reverse=True)


def create_backup(settings: Settings) -> BackupFile:
    """Snapshot the live SQLite file via the backup API (safe against a writer
    holding the file open — a plain file copy is not)."""
    src_path = sqlite_path(settings)
    if src_path is None:
        raise ValueError("backups are only supported for a sqlite database_url")
    d = backups_dir(settings)
    assert d is not None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest_path = d / f"orchestrator-{stamp}.db"

    src = sqlite3.connect(str(src_path))
    dest = sqlite3.connect(str(dest_path))
    try:
        src.backup(dest)
    finally:
        dest.close()
        src.close()
    stat = dest_path.stat()
    return BackupFile(name=dest_path.name, size_bytes=stat.st_size,
                       created_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc))


def delete_backup(settings: Settings, name: str) -> bool:
    d = backups_dir(settings)
    if d is None:
        return False
    target = (d / name).resolve()
    if target.parent != d.resolve() or not target.exists():
        return False
    target.unlink()
    return True


def vacuum(settings: Settings) -> None:
    path = sqlite_path(settings)
    if path is None:
        raise ValueError("vacuum is only supported for a sqlite database_url")
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()


def purge_old(db: Session, *, older_than_days: int) -> dict:
    """Delete ``Job`` (cascades to its ``Listing`` rows) and ``PriceObservation``
    rows older than the cutoff. Never touches ``tracked_listing`` /
    ``listing_price_point`` — that's the durable per-listing price history."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    jobs = db.scalars(select(Job).where(Job.created_at < cutoff)).all()
    n_jobs = len(jobs)
    for j in jobs:
        db.delete(j)
    n_obs = db.scalar(
        select(func.count()).select_from(PriceObservation).where(PriceObservation.observed_at < cutoff)
    ) or 0
    db.execute(delete(PriceObservation).where(PriceObservation.observed_at < cutoff))
    return {"jobs_deleted": n_jobs, "observations_deleted": n_obs}
