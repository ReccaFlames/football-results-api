"""store the season format and start month per competition

Revision ID: 0008_competition_season_format
Revises: 0007_stable_match_teams
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_competition_season_format"
down_revision: str | None = "0007_stable_match_teams"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing competitions all use YYYY-YYYY seasons that start in July.
    op.add_column(
        "competition",
        sa.Column(
            "season_format", sa.String(10), server_default=sa.text("'split'"), nullable=False
        ),
    )
    op.add_column(
        "competition",
        sa.Column(
            "season_start_month", sa.SmallInteger(), server_default=sa.text("7"), nullable=False
        ),
    )
    op.create_check_constraint(
        "ck_competition_season_format",
        "competition",
        "season_format IN ('split', 'calendar')",
    )
    op.create_check_constraint(
        "ck_competition_season_start_month",
        "competition",
        "season_start_month BETWEEN 1 AND 12",
    )


def downgrade() -> None:
    op.drop_constraint("ck_competition_season_start_month", "competition", type_="check")
    op.drop_constraint("ck_competition_season_format", "competition", type_="check")
    op.drop_column("competition", "season_start_month")
    op.drop_column("competition", "season_format")
