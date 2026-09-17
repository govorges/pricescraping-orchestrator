"""add tracked_listing.hidden

Revision ID: 0005_tracked_listing_hidden
Revises: 0004_reclassify_listing_type
Create Date: 2026-09-10

The per-query listings page now shows one row per tracked listing (deduplicated
across scrape runs), so the operator-hide flag moves to ``tracked_listing``.
Guarded on the inspector like the earlier additive migrations.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_tracked_listing_hidden"
down_revision: str | None = "0004_reclassify_listing_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("tracked_listing")}
    indexes = {ix["name"] for ix in inspector.get_indexes("tracked_listing")}

    if "hidden" not in columns:
        with op.batch_alter_table("tracked_listing") as batch_op:
            batch_op.add_column(
                sa.Column("hidden", sa.Boolean(), nullable=False, server_default=sa.false())
            )
    if "ix_tracked_listing_hidden" not in indexes:
        op.create_index("ix_tracked_listing_hidden", "tracked_listing", ["hidden"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {ix["name"] for ix in inspector.get_indexes("tracked_listing")}
    if "ix_tracked_listing_hidden" in indexes:
        op.drop_index("ix_tracked_listing_hidden", table_name="tracked_listing")
    with op.batch_alter_table("tracked_listing") as batch_op:
        batch_op.drop_column("hidden")
