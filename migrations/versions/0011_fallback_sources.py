"""fallback sources: per-season fallback URL, primary failure count, source of the last import

Revision ID: 0011_fallback_sources
Revises: 0010_scrape_run_worker
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_fallback_sources"
down_revision: str | None = "0010_scrape_run_worker"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "competition_season", sa.Column("fallback_provider", sa.String(length=30), nullable=True)
    )
    op.add_column("competition_season", sa.Column("fallback_url", sa.Text(), nullable=True))
    op.add_column(
        "competition_season",
        sa.Column(
            "primary_consecutive_failures",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "competition_season",
        sa.Column("last_success_provider", sa.String(length=30), nullable=True),
    )
    op.add_column("competition_season", sa.Column("last_success_url", sa.Text(), nullable=True))
    # Until now every import came from the primary source.
    op.execute(
        "UPDATE competition_season SET primary_consecutive_failures = consecutive_failures, "
        "last_success_provider = CASE WHEN fetched_at IS NULL THEN NULL ELSE provider END, "
        "last_success_url = CASE WHEN fetched_at IS NULL THEN NULL ELSE source_url END"
    )
    # NULL for runs created before this revision.
    op.add_column("scrape_run", sa.Column("provider", sa.String(length=30), nullable=True))
    op.add_column(
        "scrape_run",
        sa.Column("fallback", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("scrape_run", "fallback")
    op.drop_column("scrape_run", "provider")
    op.drop_column("competition_season", "last_success_url")
    op.drop_column("competition_season", "last_success_provider")
    op.drop_column("competition_season", "primary_consecutive_failures")
    op.drop_column("competition_season", "fallback_url")
    op.drop_column("competition_season", "fallback_provider")
