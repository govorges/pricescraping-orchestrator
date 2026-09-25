"""recompute price_observation.price_low without auction listings

Revision ID: 0010_price_low_excludes_auctions
Revises: 0009_benchmark_snapshot
Create Date: 2026-09-25

Ingest now drops auction listings when computing ``price_low`` (a current bid
isn't a price anyone can buy at). Recompute it for existing observations from
the listings already stored against each job. Observations whose job has no
stored listings keep the worker-reported value -- there's nothing to filter.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy.orm import Session

from orchestrator.api.ingest import lowest_non_auction_price
from orchestrator.models import Listing, PriceObservation

revision: str = "0010_price_low_excludes_auctions"
down_revision: str | None = "0009_benchmark_snapshot"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    session = Session(bind=op.get_bind())

    by_job: dict[str, list[tuple[float, str]]] = {}
    for job_id, price, listing_type in session.query(
        Listing.job_id, Listing.price, Listing.listing_type
    ):
        by_job.setdefault(job_id, []).append((price, listing_type))

    for obs in session.query(PriceObservation).filter(PriceObservation.job_id.in_(by_job)):
        obs.price_low = lowest_non_auction_price(by_job[obs.job_id])

    session.commit()


def downgrade() -> None:
    # The worker-reported values weren't kept; leave the recomputed ones.
    pass
