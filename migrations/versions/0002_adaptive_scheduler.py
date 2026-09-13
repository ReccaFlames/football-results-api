"""Add adaptive fixture scheduler.

Revision ID: 0002_adaptive_scheduler
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_adaptive_scheduler"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "competition_season",
        sa.Column(
            "automatic_refresh_enabled", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
    )
    op.add_column(
        "competition_season",
        sa.Column("scheduler_state", sa.String(30), server_default="disabled", nullable=False),
    )
    op.add_column(
        "competition_season", sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "competition_season",
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "competition_season",
        sa.Column("last_successful_check_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "competition_season",
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_index("ix_competition_season_next_check_at", "competition_season", ["next_check_at"])
    op.execute("""UPDATE competition_season SET automatic_refresh_enabled = true,
        scheduler_state = 'waiting_for_schedule', next_check_at = now() WHERE is_current""")

    op.add_column(
        "match_result",
        sa.Column("status", sa.String(20), server_default="finished", nullable=False),
    )
    op.alter_column("match_result", "home_score", existing_type=sa.Integer(), nullable=True)
    op.alter_column("match_result", "away_score", existing_type=sa.Integer(), nullable=True)
    op.create_index("ix_match_result_status", "match_result", ["status"])

    op.create_table(
        "scheduler_heartbeat",
        sa.Column("worker_id", sa.String(200), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_scheduler_heartbeat_heartbeat_at", "scheduler_heartbeat", ["heartbeat_at"])


def downgrade() -> None:
    op.drop_index("ix_scheduler_heartbeat_heartbeat_at", table_name="scheduler_heartbeat")
    op.drop_table("scheduler_heartbeat")
    op.drop_index("ix_match_result_status", table_name="match_result")
    op.drop_column("match_result", "status")
    op.execute("DELETE FROM match_result WHERE home_score IS NULL OR away_score IS NULL")
    op.alter_column("match_result", "away_score", existing_type=sa.Integer(), nullable=False)
    op.alter_column("match_result", "home_score", existing_type=sa.Integer(), nullable=False)
    op.drop_index("ix_competition_season_next_check_at", table_name="competition_season")
    for name in (
        "consecutive_failures",
        "last_successful_check_at",
        "last_checked_at",
        "next_check_at",
        "scheduler_state",
        "automatic_refresh_enabled",
    ):
        op.drop_column("competition_season", name)
