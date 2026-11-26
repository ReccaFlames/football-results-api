"""access requests: requests for an API key on a private instance (ADR 0010)

Revision ID: 0013_access_requests
Revises: 0012_scrape_run_matches_skipped
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_access_requests"
down_revision: str | None = "0012_scrape_run_matches_skipped"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "access_request",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("client_type", sa.String(length=10), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("contact_email", sa.String(length=320), nullable=True),
        sa.Column("intended_use", sa.String(length=1000), nullable=True),
        sa.Column("competitions", postgresql.ARRAY(sa.String(length=100)), nullable=True),
        sa.Column("message", sa.String(length=2000), nullable=True),
        sa.Column("claim_secret_hash", sa.String(length=71), nullable=False),
        sa.Column("key_name", sa.String(length=200), nullable=True),
        sa.Column("api_key", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'claimed', 'expired')",
            name="ck_access_request_status",
        ),
        sa.CheckConstraint(
            "client_type IN ('human', 'agent')", name="ck_access_request_client_type"
        ),
        sa.CheckConstraint(
            "api_key IS NULL OR status = 'approved'", name="ck_access_request_api_key"
        ),
        sa.CheckConstraint(
            "status IN ('claimed', 'expired') OR (name IS NOT NULL "
            "AND contact_email IS NOT NULL AND intended_use IS NOT NULL)",
            name="ck_access_request_personal_data",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_access_request_created_at", "access_request", ["created_at"])
    op.create_index(
        "uq_access_request_pending_email",
        "access_request",
        ["contact_email"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("uq_access_request_pending_email", table_name="access_request")
    op.drop_index("ix_access_request_created_at", table_name="access_request")
    op.drop_table("access_request")
