"""queue scrape jobs

Revision ID: 0005_queued_scrape_jobs
Revises: 0004_match_statistics
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_queued_scrape_jobs"
down_revision: str | None = "0004_match_statistics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("scrape_run", sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE scrape_run SET queued_at = started_at")
    op.alter_column(
        "scrape_run",
        "queued_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )
    op.alter_column(
        "scrape_run",
        "started_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
    )
    op.create_index(
        "ix_scrape_run_status_queued_at", "scrape_run", ["status", "queued_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_scrape_run_status_queued_at", table_name="scrape_run")
    op.execute(
        "UPDATE scrape_run SET started_at = queued_at WHERE started_at IS NULL AND queued_at IS NOT NULL"
    )
    op.alter_column(
        "scrape_run",
        "started_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )
    op.drop_column("scrape_run", "queued_at")
