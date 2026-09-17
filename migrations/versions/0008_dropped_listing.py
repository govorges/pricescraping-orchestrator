"""add dropped_listing table

Revision ID: 0008_dropped_listing
Revises: 0007_job_node_id
Create Date: 2026-09-14

Tracks *why* a listing the worker's selector found on the page didn't make it
into ``listing`` -- kept as its own table rather than a nullable-price row on
``listing`` so it can never leak into the price-sorted ``/api/v1/listings``
browsing endpoint, which assumes every row is a real, priced listing. Guarded
on the inspector for the same reason as the earlier additive migrations.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import orchestrator.models  # noqa: F401,E402
from orchestrator.db import Base

revision: str = "0008_dropped_listing"
down_revision: str | None = "0007_job_node_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "dropped_listing" not in inspector.get_table_names():
        Base.metadata.tables["dropped_listing"].create(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "dropped_listing" in inspector.get_table_names():
        Base.metadata.tables["dropped_listing"].drop(bind=bind)
