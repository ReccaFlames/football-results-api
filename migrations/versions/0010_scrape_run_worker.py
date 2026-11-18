"""record which scheduler worker runs a scrape so abandoned runs can be failed

Revision ID: 0010_scrape_run_worker
Revises: 0009_competition_season_check
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_scrape_run_worker"
down_revision: str | None = "0009_competition_season_check"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NULL for runs created before this revision and for runs outside the scheduler.
    op.add_column("scrape_run", sa.Column("worker_id", sa.String(length=200), nullable=True))


def downgrade() -> None:
    op.drop_column("scrape_run", "worker_id")
