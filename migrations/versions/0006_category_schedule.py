"""category-wide schedules

Revision ID: 0006_category_schedule
Revises: 0005_tracked_listing_hidden
Create Date: 2026-09-11

Adds ``category_schedule`` (a cron rule that fans out to every query object in
a category, against one vendor or every enabled vendor) and the column that
links a spawned ``job`` back to the rule that created it. Guarded on the
inspector for the same reason as the earlier additive migrations.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import orchestrator.models  # noqa: F401,E402
from orchestrator.db import Base

revision: str = "0006_category_schedule"
down_revision: str | None = "0005_tracked_listing_hidden"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "category_schedule" not in inspector.get_table_names():
        Base.metadata.tables["category_schedule"].create(bind=bind)

    job_columns = {c["name"] for c in inspector.get_columns("job")}
    if "category_schedule_id" not in job_columns:
        with op.batch_alter_table("job") as batch_op:
            batch_op.add_column(sa.Column("category_schedule_id", sa.Integer(), nullable=True))
            batch_op.create_foreign_key(
                "fk_job_category_schedule", "category_schedule", ["category_schedule_id"], ["id"],
                ondelete="SET NULL",
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    job_columns = {c["name"] for c in inspector.get_columns("job")}
    if "category_schedule_id" in job_columns:
        with op.batch_alter_table("job") as batch_op:
            batch_op.drop_column("category_schedule_id")

    if "category_schedule" in inspector.get_table_names():
        Base.metadata.tables["category_schedule"].drop(bind=bind)
