"""Run the anomaly checks over runs already in the DB.

Usage:
    python scripts/backfill_anomalies.py [--days N]

Detection normally happens as each result is ingested, so runs from before the
checks existed have none. This walks them oldest first -- each run compared
with the runs before it, exactly as ingest would have -- and applies each
rule's automatic action (e.g. leaving a broken-looking run out of trends).
Idempotent: an anomaly already raised for a run is never raised again. Run
``alembic upgrade head`` first.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from orchestrator.anomalies import detect_for_run
from orchestrator.db import session_scope
from orchestrator.models import PriceObservation


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=None, help="only runs from the last N days")
    args = parser.parse_args()

    raised: Counter = Counter()
    with session_scope() as db:
        stmt = select(PriceObservation.job_id).order_by(PriceObservation.observed_at.asc())
        if args.days:
            stmt = stmt.where(PriceObservation.observed_at >= datetime.now(timezone.utc) - timedelta(days=args.days))
        job_ids = db.scalars(stmt).all()
        for job_id in job_ids:
            for a in detect_for_run(db, job_id):
                raised[a.kind] += 1
            db.flush()  # later runs compare against this one's outcome (e.g. left out of trends)

    summary = ", ".join(f"{n} {kind}" for kind, n in sorted(raised.items())) or "nothing new"
    print(f"checked {len(job_ids)} run(s): {summary}")


if __name__ == "__main__":
    main()
