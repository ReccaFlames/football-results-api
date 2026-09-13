"""Add optional match statistics.

Revision ID: 0004_match_statistics
Revises: 0003_source_provider
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_match_statistics"
down_revision: str | None = "0003_source_provider"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INTEGER_COLUMNS = (
    "home_goals",
    "away_goals",
    "home_goals_first_half",
    "away_goals_first_half",
    "home_goals_second_half",
    "away_goals_second_half",
    "home_corners",
    "away_corners",
    "home_cards",
    "away_cards",
    "home_fouls",
    "away_fouls",
)


def upgrade() -> None:
    for name in INTEGER_COLUMNS:
        op.add_column("match_result", sa.Column(name, sa.Integer(), nullable=True))
    op.add_column("match_result", sa.Column("first_team_to_score", sa.String(200), nullable=True))
    op.add_column("match_result", sa.Column("first_goal_minute", sa.String(10), nullable=True))
    op.execute(
        "UPDATE match_result SET home_goals = home_score, away_goals = away_score "
        "WHERE status = 'finished'"
    )


def downgrade() -> None:
    op.drop_column("match_result", "first_goal_minute")
    op.drop_column("match_result", "first_team_to_score")
    for name in reversed(INTEGER_COLUMNS):
        op.drop_column("match_result", name)
