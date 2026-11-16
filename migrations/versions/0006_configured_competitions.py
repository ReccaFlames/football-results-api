"""enable and disable configured competitions

Revision ID: 0006_configured_competitions
Revises: 0005_queued_scrape_jobs
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_configured_competitions"
down_revision: str | None = "0005_queued_scrape_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "competition",
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("competition", "is_enabled")
