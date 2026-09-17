"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-03

Creates every table from the ORM metadata (kept in one place: orchestrator/models.py).
On PostgreSQL it additionally promotes ``price_observation`` to a TimescaleDB
hypertable when the extension is available; on SQLite that step is skipped.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import orchestrator.models  # noqa: F401  (register tables on Base.metadata)
from orchestrator.db import Base

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)

    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        try:
            op.execute(
                "SELECT create_hypertable('price_observation', 'observed_at', "
                "if_not_exists => TRUE, migrate_data => TRUE)"
            )
        except sa.exc.SQLAlchemyError:
            # Timescale not installed on this Postgres — plain table is fine.
            pass


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
