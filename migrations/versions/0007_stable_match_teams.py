"""add stable match identity and team references

Revision ID: 0007_stable_match_teams
Revises: 0006_configured_competitions
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_stable_match_teams"
down_revision: str | None = "0006_configured_competitions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "team",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("slug", sa.String(length=200), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_team_slug"),
    )
    op.create_table(
        "team_alias",
        sa.Column("normalized_alias", sa.String(length=250), nullable=False),
        sa.Column("alias", sa.String(length=200), nullable=False),
        sa.Column("team_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["team.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("normalized_alias"),
    )
    op.create_index("ix_team_alias_team_id", "team_alias", ["team_id"])
    op.add_column("match_result", sa.Column("provider_match_id", sa.String(length=100)))
    op.add_column("match_result", sa.Column("home_team_id", sa.BigInteger()))
    op.add_column("match_result", sa.Column("away_team_id", sa.BigInteger()))

    op.execute(
        """
        WITH all_names AS (
            SELECT home_team AS name FROM match_result
            UNION ALL
            SELECT away_team AS name FROM match_result
        ),
        canonical_names AS (
            SELECT lower(regexp_replace(trim(name), '[[:space:]]+', ' ', 'g')) AS normalized_alias,
                   min(trim(name)) AS name
            FROM all_names
            GROUP BY lower(regexp_replace(trim(name), '[[:space:]]+', ' ', 'g'))
        ),
        base_slugs AS (
            SELECT normalized_alias,
                   name,
                   coalesce(
                       nullif(trim(both '-' FROM regexp_replace(
                           translate(lower(name), 'ąćęłńóśźż', 'acelnoszz'),
                           '[^a-z0-9]+',
                           '-',
                           'g'
                       )), ''),
                       'team'
                   ) AS base_slug
            FROM canonical_names
        ),
        slugs AS (
            SELECT normalized_alias,
                   name,
                   CASE
                       WHEN count(*) OVER (PARTITION BY base_slug) = 1 THEN base_slug
                       ELSE left(base_slug, 189) || '-' || left(md5(normalized_alias), 10)
                   END AS slug
            FROM base_slugs
        )
        INSERT INTO team (slug, name, created_at, updated_at)
        SELECT slug, name, now(), now() FROM slugs
        """
    )
    op.execute(
        """
        WITH all_names AS (
            SELECT home_team AS name FROM match_result
            UNION ALL
            SELECT away_team AS name FROM match_result
        ),
        canonical_names AS (
            SELECT lower(regexp_replace(trim(name), '[[:space:]]+', ' ', 'g')) AS normalized_alias,
                   min(trim(name)) AS alias
            FROM all_names
            GROUP BY lower(regexp_replace(trim(name), '[[:space:]]+', ' ', 'g'))
        ),
        base_slugs AS (
            SELECT normalized_alias,
                   alias,
                   coalesce(
                       nullif(trim(both '-' FROM regexp_replace(
                           translate(lower(alias), 'ąćęłńóśźż', 'acelnoszz'),
                           '[^a-z0-9]+',
                           '-',
                           'g'
                       )), ''),
                       'team'
                   ) AS base_slug
            FROM canonical_names
        ),
        slugs AS (
            SELECT normalized_alias,
                   alias,
                   CASE
                       WHEN count(*) OVER (PARTITION BY base_slug) = 1 THEN base_slug
                       ELSE left(base_slug, 189) || '-' || left(md5(normalized_alias), 10)
                   END AS slug
            FROM base_slugs
        )
        INSERT INTO team_alias (normalized_alias, alias, team_id)
        SELECT slugs.normalized_alias, slugs.alias, team.id
        FROM slugs
        JOIN team ON team.slug = slugs.slug
        """
    )
    op.execute(
        """
        UPDATE match_result
        SET home_team_id = home_alias.team_id,
            away_team_id = away_alias.team_id
        FROM competition_season,
             team_alias AS home_alias,
             team_alias AS away_alias
        WHERE competition_season.id = match_result.competition_season_id
          AND home_alias.normalized_alias =
              lower(regexp_replace(trim(match_result.home_team), '[[:space:]]+', ' ', 'g'))
          AND away_alias.normalized_alias =
              lower(regexp_replace(trim(match_result.away_team), '[[:space:]]+', ' ', 'g'))
        """
    )
    op.execute(
        """
        UPDATE match_result
        SET provider_match_id = CASE
                WHEN competition_season.provider = '90minut'
                    THEN substring(match_result.source_url FROM '[?&]id_mecz=([0-9]+)')
                WHEN competition_season.provider = 'football-data'
                    THEN substring(match_result.source_url FROM '/matches/([0-9]+)(/|$)')
                ELSE NULL
            END,
            source_key = competition_season.season || '|' ||
                coalesce(match_result.round::text, 'None') || '|' ||
                match_result.home_team_id::text || '|' || match_result.away_team_id::text
        FROM competition_season
        WHERE competition_season.id = match_result.competition_season_id
        """
    )

    op.alter_column(
        "match_result",
        "home_team_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
    op.alter_column(
        "match_result",
        "away_team_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
    op.create_foreign_key(
        "fk_match_result_home_team_id_team",
        "match_result",
        "team",
        ["home_team_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_match_result_away_team_id_team",
        "match_result",
        "team",
        ["away_team_id"],
        ["id"],
    )
    op.create_unique_constraint(
        "uq_match_result_provider_match_id",
        "match_result",
        ["competition_season_id", "provider_match_id"],
    )
    op.create_index("ix_match_result_home_team_id", "match_result", ["home_team_id"])
    op.create_index("ix_match_result_away_team_id", "match_result", ["away_team_id"])


def downgrade() -> None:
    op.drop_index("ix_match_result_away_team_id", table_name="match_result")
    op.drop_index("ix_match_result_home_team_id", table_name="match_result")
    op.drop_constraint(
        "uq_match_result_provider_match_id",
        "match_result",
        type_="unique",
    )
    op.drop_constraint("fk_match_result_away_team_id_team", "match_result", type_="foreignkey")
    op.drop_constraint("fk_match_result_home_team_id_team", "match_result", type_="foreignkey")
    op.drop_column("match_result", "away_team_id")
    op.drop_column("match_result", "home_team_id")
    op.drop_column("match_result", "provider_match_id")
    op.drop_index("ix_team_alias_team_id", table_name="team_alias")
    op.drop_table("team_alias")
    op.drop_table("team")
