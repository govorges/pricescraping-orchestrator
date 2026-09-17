"""categories + listing tracking (auction/buy_now classification, price-over-time)

Revision ID: 0003_categories_and_tracking
Revises: 0002_listing_hidden
Create Date: 2026-09-04

Adds ``category`` (scoped extra-exclude phrases + price guard rails for a group
of query objects), ``tracked_listing`` + ``listing_price_point`` (one real-world
listing followed across scrapes by its canonicalised URL), and the columns that
link existing tables to them. Guarded with the inspector for the same reason as
``0002_listing_hidden``: ``0001_initial`` builds from the *current* ORM metadata,
so a database created fresh from ``0001`` already has all of this.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# register tables on Base.metadata
import orchestrator.models  # noqa: F401,E402
from orchestrator.db import Base

revision: str = "0003_categories_and_tracking"
down_revision: str | None = "0002_listing_hidden"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_TABLES = ("category", "tracked_listing", "listing_price_point")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    for name in _NEW_TABLES:
        if name not in existing_tables:
            Base.metadata.tables[name].create(bind=bind)

    qo_columns = {c["name"] for c in inspector.get_columns("query_object")}
    if "category_id" not in qo_columns:
        with op.batch_alter_table("query_object") as batch_op:
            batch_op.add_column(sa.Column("category_id", sa.String(length=128), nullable=True))
            batch_op.create_foreign_key(
                "fk_query_object_category", "category", ["category_id"], ["id"], ondelete="SET NULL"
            )
        op.create_index("ix_query_object_category_id", "query_object", ["category_id"])

    listing_columns = {c["name"] for c in inspector.get_columns("listing")}
    listing_indexes = {ix["name"] for ix in inspector.get_indexes("listing")}
    if "listing_type" not in listing_columns:
        with op.batch_alter_table("listing") as batch_op:
            batch_op.add_column(
                sa.Column("listing_type", sa.String(length=16), nullable=False,
                         server_default="unknown")
            )
        op.create_index("ix_listing_listing_type", "listing", ["listing_type"])
    if "tracked_listing_id" not in listing_columns:
        with op.batch_alter_table("listing") as batch_op:
            batch_op.add_column(sa.Column("tracked_listing_id", sa.Integer(), nullable=True))
            batch_op.create_foreign_key(
                "fk_listing_tracked_listing", "tracked_listing", ["tracked_listing_id"], ["id"],
                ondelete="SET NULL",
            )
    if "ix_listing_tracked_listing_id" not in listing_indexes:
        op.create_index("ix_listing_tracked_listing_id", "listing", ["tracked_listing_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    listing_indexes = {ix["name"] for ix in inspector.get_indexes("listing")}
    if "ix_listing_tracked_listing_id" in listing_indexes:
        op.drop_index("ix_listing_tracked_listing_id", table_name="listing")
    if "ix_listing_listing_type" in listing_indexes:
        op.drop_index("ix_listing_listing_type", table_name="listing")
    with op.batch_alter_table("listing") as batch_op:
        batch_op.drop_column("tracked_listing_id")
        batch_op.drop_column("listing_type")

    qo_indexes = {ix["name"] for ix in inspector.get_indexes("query_object")}
    if "ix_query_object_category_id" in qo_indexes:
        op.drop_index("ix_query_object_category_id", table_name="query_object")
    with op.batch_alter_table("query_object") as batch_op:
        batch_op.drop_column("category_id")

    for name in ("listing_price_point", "tracked_listing", "category"):
        if name in inspector.get_table_names():
            Base.metadata.tables[name].drop(bind=bind)
