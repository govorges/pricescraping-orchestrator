"""add listing.hidden

Revision ID: 0002_listing_hidden
Revises: 0001_initial
Create Date: 2026-09-04

``0001_initial`` builds its schema from the *current* ORM metadata
(``Base.metadata.create_all``), so a database created fresh from ``0001`` already
has this column — only a database that was migrated to ``0001`` before ``hidden``
was added to :class:`orchestrator.models.Listing` is actually missing it. Guard
on the inspector so ``upgrade head`` is safe either way.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_listing_hidden"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("listing")}
    indexes = {ix["name"] for ix in inspector.get_indexes("listing")}

    if "hidden" not in columns:
        with op.batch_alter_table("listing") as batch_op:
            batch_op.add_column(
                sa.Column("hidden", sa.Boolean(), nullable=False, server_default=sa.false())
            )
    if "ix_listing_hidden" not in indexes:
        op.create_index("ix_listing_hidden", "listing", ["hidden"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {ix["name"] for ix in inspector.get_indexes("listing")}
    if "ix_listing_hidden" in indexes:
        op.drop_index("ix_listing_hidden", table_name="listing")
    with op.batch_alter_table("listing") as batch_op:
        batch_op.drop_column("hidden")
