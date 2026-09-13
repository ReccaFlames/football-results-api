"""Add PostgreSQL persistence for competitions and results.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "competition",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("country", sa.String(length=100), nullable=True),
        sa.Column("format", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index("ix_competition_slug", "competition", ["slug"])
    op.create_table(
        "competition_season",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("competition_id", sa.BigInteger(), nullable=False),
        sa.Column("season", sa.String(length=9), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["competition_id"], ["competition.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("competition_id", "season", name="uq_competition_season"),
    )
    op.create_index(
        "ix_competition_season_competition_id", "competition_season", ["competition_id"]
    )
    op.create_index(
        "uq_competition_season_current",
        "competition_season",
        ["competition_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )
    op.create_table(
        "match_result",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("competition_season_id", sa.BigInteger(), nullable=False),
        sa.Column("source_key", sa.String(length=64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("stage", sa.String(length=250), nullable=False),
        sa.Column("round", sa.Integer(), nullable=True),
        sa.Column("home_team", sa.String(length=200), nullable=False),
        sa.Column("away_team", sa.String(length=200), nullable=False),
        sa.Column("home_score", sa.Integer(), nullable=False),
        sa.Column("away_score", sa.Integer(), nullable=False),
        sa.Column("played_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["competition_season_id"], ["competition_season.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "competition_season_id", "source_key", name="uq_match_result_source_key"
        ),
    )
    op.create_index(
        "ix_match_result_competition_season_id", "match_result", ["competition_season_id"]
    )
    op.create_index(
        "ix_match_result_season_round", "match_result", ["competition_season_id", "round"]
    )
    op.create_index("ix_match_result_played_at", "match_result", ["played_at"])
    op.create_index("ix_match_result_home_team", "match_result", ["home_team"])
    op.create_index("ix_match_result_away_team", "match_result", ["away_team"])
    op.create_table(
        "scrape_run",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("competition_season_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("matches_found", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["competition_season_id"], ["competition_season.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scrape_run_competition_season_id", "scrape_run", ["competition_season_id"])
    op.create_index(
        "ix_scrape_run_season_started", "scrape_run", ["competition_season_id", "started_at"]
    )


def downgrade() -> None:
    op.drop_table("scrape_run")
    op.drop_table("match_result")
    op.drop_table("competition_season")
    op.drop_table("competition")
