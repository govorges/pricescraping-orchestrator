"""add query_object.dynamic_band

Revision ID: 0012_query_dynamic_band
Revises: 0011_anomalies
Create Date: 2026-09-26

With ``dynamic_band`` on, each job's price band follows that vendor's recent
median price (``orchestrator.bands``) instead of the query object's fixed
minimum/maximum. Off by default, so existing bands are unchanged. Guarded on
the inspector for the same reason as the earlier additive migrations.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_query_dynamic_band"
down_revision: str | None = "0011_anomalies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {c["name"] for c in sa.inspect(bind).get_columns("query_object")}
    if "dynamic_band" not in columns:
        with op.batch_alter_table("query_object") as batch_op:
            batch_op.add_column(
                sa.Column("dynamic_band", sa.Boolean(), nullable=False, server_default=sa.false())
            )


def downgrade() -> None:
    with op.batch_alter_table("query_object") as batch_op:
        batch_op.drop_column("dynamic_band")
