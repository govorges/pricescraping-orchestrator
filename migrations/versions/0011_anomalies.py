"""anomaly detection: anomaly + anomaly_rule_set tables, price_observation.excluded

Revision ID: 0011_anomalies
Revises: 0010_price_low_excludes_auctions
Create Date: 2026-09-26

Anomalies (outlier listings, median moves, listing price cuts, broken-looking
runs) are raised on ingest by ``orchestrator.anomalies``. A run judged
untrustworthy can be left out of price trends, which is what the new
``price_observation.excluded`` flag records. Guarded on the inspector for the
same reason as the earlier additive migrations.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import orchestrator.models  # noqa: F401,E402
from orchestrator.db import Base

revision: str = "0011_anomalies"
down_revision: str | None = "0010_price_low_excludes_auctions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("anomaly", "anomaly_rule_set")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())
    for name in _TABLES:
        if name not in existing:
            Base.metadata.tables[name].create(bind=bind)

    columns = {c["name"] for c in inspector.get_columns("price_observation")}
    indexes = {ix["name"] for ix in inspector.get_indexes("price_observation")}
    if "excluded" not in columns:
        with op.batch_alter_table("price_observation") as batch_op:
            batch_op.add_column(
                sa.Column("excluded", sa.Boolean(), nullable=False, server_default=sa.false())
            )
    if "ix_price_observation_excluded" not in indexes:
        op.create_index("ix_price_observation_excluded", "price_observation", ["excluded"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {ix["name"] for ix in inspector.get_indexes("price_observation")}
    if "ix_price_observation_excluded" in indexes:
        op.drop_index("ix_price_observation_excluded", table_name="price_observation")
    with op.batch_alter_table("price_observation") as batch_op:
        batch_op.drop_column("excluded")
    existing = set(inspector.get_table_names())
    for name in reversed(_TABLES):
        if name in existing:
            Base.metadata.tables[name].drop(bind=bind)
