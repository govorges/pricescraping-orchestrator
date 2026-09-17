"""add job.node_id

Revision ID: 0007_job_node_id
Revises: 0006_category_schedule
Create Date: 2026-09-12

The "jobs / min" figure on a worker node's capacity card was always reading
``node.meta['jobs_per_min']``, a key nothing ever wrote -- always showing "-".
A reported result already carries which node ran it (``worker.node`` in the
ingest payload), it just wasn't being stored. This adds the column so
``orchestrator.api.ingest`` can record it and the nodes view can compute a
real rate. Guarded on the inspector for the same reason as the earlier
additive migrations.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_job_node_id"
down_revision: str | None = "0006_category_schedule"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("job")}
    indexes = {ix["name"] for ix in inspector.get_indexes("job")}

    if "node_id" not in columns:
        with op.batch_alter_table("job") as batch_op:
            batch_op.add_column(sa.Column("node_id", sa.String(length=128), nullable=True))
    if "ix_job_node_id" not in indexes:
        op.create_index("ix_job_node_id", "job", ["node_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {ix["name"] for ix in inspector.get_indexes("job")}
    if "ix_job_node_id" in indexes:
        op.drop_index("ix_job_node_id", table_name="job")
    with op.batch_alter_table("job") as batch_op:
        batch_op.drop_column("node_id")
