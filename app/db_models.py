from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class CompetitionRow(Base):
    __tablename__ = "competition"
    # Mirrors migration 0001: a unique constraint plus a separate non-unique index.
    __table_args__ = (
        UniqueConstraint("slug", name="competition_slug_key"),
        Index("ix_competition_slug", "slug"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))
    country: Mapped[str | None] = mapped_column(String(100))
    format: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    seasons: Mapped[list["CompetitionSeasonRow"]] = relationship(
        back_populates="competition",
        cascade="all, delete-orphan",
    )


class CompetitionSeasonRow(Base):
    __tablename__ = "competition_season"
    __table_args__ = (
        UniqueConstraint("competition_id", "season", name="uq_competition_season"),
        Index(
            "uq_competition_season_current",
            "competition_id",
            unique=True,
            postgresql_where=text("is_current"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    competition_id: Mapped[int] = mapped_column(
        ForeignKey("competition.id", ondelete="CASCADE"),
        index=True,
    )
    season: Mapped[str] = mapped_column(String(9))
    source_url: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(30), default="90minut")
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    automatic_refresh_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    scheduler_state: Mapped[str] = mapped_column(String(30), default="disabled")
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_successful_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    competition: Mapped[CompetitionRow] = relationship(back_populates="seasons")
    matches: Mapped[list["MatchRow"]] = relationship(
        back_populates="competition_season",
        cascade="all, delete-orphan",
    )
    scrape_runs: Mapped[list["ScrapeRunRow"]] = relationship(
        back_populates="competition_season",
        cascade="all, delete-orphan",
    )


class MatchRow(Base):
    __tablename__ = "match_result"
    __table_args__ = (
        UniqueConstraint(
            "competition_season_id",
            "source_key",
            name="uq_match_result_source_key",
        ),
        Index("ix_match_result_season_round", "competition_season_id", "round"),
        Index("ix_match_result_played_at", "played_at"),
        Index("ix_match_result_home_team", "home_team"),
        Index("ix_match_result_away_team", "away_team"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    competition_season_id: Mapped[int] = mapped_column(
        ForeignKey("competition_season.id", ondelete="CASCADE"),
        index=True,
    )
    source_key: Mapped[str] = mapped_column(String(64))
    source_url: Mapped[str] = mapped_column(Text)
    stage: Mapped[str] = mapped_column(String(250))
    round: Mapped[int | None] = mapped_column(Integer)
    home_team: Mapped[str] = mapped_column(String(200))
    away_team: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="finished", index=True)
    home_score: Mapped[int | None] = mapped_column(Integer)
    away_score: Mapped[int | None] = mapped_column(Integer)
    home_goals: Mapped[int | None] = mapped_column(Integer)
    away_goals: Mapped[int | None] = mapped_column(Integer)
    home_goals_first_half: Mapped[int | None] = mapped_column(Integer)
    away_goals_first_half: Mapped[int | None] = mapped_column(Integer)
    home_goals_second_half: Mapped[int | None] = mapped_column(Integer)
    away_goals_second_half: Mapped[int | None] = mapped_column(Integer)
    home_corners: Mapped[int | None] = mapped_column(Integer)
    away_corners: Mapped[int | None] = mapped_column(Integer)
    home_cards: Mapped[int | None] = mapped_column(Integer)
    away_cards: Mapped[int | None] = mapped_column(Integer)
    home_fouls: Mapped[int | None] = mapped_column(Integer)
    away_fouls: Mapped[int | None] = mapped_column(Integer)
    first_team_to_score: Mapped[str | None] = mapped_column(String(200))
    first_goal_minute: Mapped[str | None] = mapped_column(String(10))
    played_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    competition_season: Mapped[CompetitionSeasonRow] = relationship(back_populates="matches")


class ScrapeRunRow(Base):
    __tablename__ = "scrape_run"
    __table_args__ = (Index("ix_scrape_run_season_started", "competition_season_id", "started_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    competition_season_id: Mapped[int] = mapped_column(
        ForeignKey("competition_season.id", ondelete="CASCADE"),
        index=True,
    )
    status: Mapped[str] = mapped_column(String(20))
    source_url: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    matches_found: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(Text)

    competition_season: Mapped[CompetitionSeasonRow] = relationship(back_populates="scrape_runs")


class SchedulerHeartbeatRow(Base):
    __tablename__ = "scheduler_heartbeat"

    worker_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
