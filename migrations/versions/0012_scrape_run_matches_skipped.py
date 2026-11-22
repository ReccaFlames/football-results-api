"""count fallback matches skipped because a team is unknown in the season

Revision ID: 0012_scrape_run_matches_skipped
Revises: 0011_fallback_sources
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_scrape_run_matches_skipped"
down_revision: str | None = "0011_fallback_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing runs skipped nothing, so the server default backfills them with 0.
    op.add_column(
        "scrape_run",
        sa.Column("matches_skipped", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("scrape_run", "matches_skipped")
