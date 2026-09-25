"""add benchmark_snapshot table

Revision ID: 0009_benchmark_snapshot
Revises: 0008_dropped_listing
Create Date: 2026-09-25

Caches the last successful fetch of an external benchmark source (Tom's
Hardware's GPU hierarchy) for the cost-per-frame page, so it is re-fetched at
most once every 24 hours. Guarded on the inspector for the same reason as the
earlier additive migrations.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import orchestrator.models  # noqa: F401,E402
from orchestrator.db import Base

revision: str = "0009_benchmark_snapshot"
down_revision: str | None = "0008_dropped_listing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "benchmark_snapshot" not in inspector.get_table_names():
        Base.metadata.tables["benchmark_snapshot"].create(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "benchmark_snapshot" in inspector.get_table_names():
        Base.metadata.tables["benchmark_snapshot"].drop(bind=bind)
