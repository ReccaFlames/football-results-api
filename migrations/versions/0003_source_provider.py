"""Persist the scraper provider for each season source.

Revision ID: 0003_source_provider
Revises: 0002_adaptive_scheduler
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_source_provider"
down_revision: str | None = "0002_adaptive_scheduler"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "competition_season",
        sa.Column("provider", sa.String(30), server_default="90minut", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("competition_season", "provider")
