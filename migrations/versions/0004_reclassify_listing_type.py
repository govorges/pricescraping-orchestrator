"""re-classify listing_type for existing rows

Revision ID: 0004_reclassify_listing_type
Revises: 0003_categories_and_tracking
Create Date: 2026-09-06

``classify_listing_type`` used to fall back to ``"unknown"`` whenever a vendor
surfaced no explicit auction / buy-now text. It now defaults to ``"buy_now"``
(a stored listing always has a resolved price; a fixed-price-only vendor can be
nothing else) and recognises many more auction signals. Recompute the stored
value on ``listing`` and ``tracked_listing`` from the text already captured.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy.orm import Session

from orchestrator.classify import classify_listing_type
from orchestrator.models import Listing, ListingPricePoint, TrackedListing

revision: str = "0004_reclassify_listing_type"
down_revision: str | None = "0003_categories_and_tracking"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    session = Session(bind=op.get_bind())

    for listing in session.query(Listing).all():
        listing.listing_type = classify_listing_type(listing.title, listing.extra)

    for tracked in session.query(TrackedListing).all():
        latest = (
            session.query(ListingPricePoint)
            .filter(ListingPricePoint.tracked_listing_id == tracked.id)
            .order_by(ListingPricePoint.observed_at.desc())
            .first()
        )
        title = (latest.title if latest else None) or tracked.title
        extra = latest.extra if latest else None
        tracked.listing_type = classify_listing_type(title, extra)

    session.commit()


def downgrade() -> None:
    # The previous classification is not recoverable; leave values as-is.
    pass
