"""schedule season discovery and rollover checks per competition

Revision ID: 0009_competition_season_check
Revises: 0008_competition_season_format
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_competition_season_check"
down_revision: str | None = "0008_competition_season_format"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NULL means the season check is due now, so existing competitions are checked once.
    op.add_column(
        "competition",
        sa.Column("next_season_check_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("competition", "next_season_check_at")
