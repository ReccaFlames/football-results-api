"""oauth server: codes, grants, and tokens of the built-in authorization server (ADR 0012)

Revision ID: 0014_oauth_server
Revises: 0013_access_requests
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_oauth_server"
down_revision: str | None = "0013_access_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_authorization_code",
        sa.Column("code_hash", sa.String(length=71), nullable=False),
        sa.Column("client_id", sa.String(length=2048), nullable=False),
        sa.Column("client_name", sa.String(length=200), nullable=False),
        sa.Column("redirect_uri", sa.String(length=2048), nullable=False),
        sa.Column("code_challenge", sa.String(length=128), nullable=False),
        sa.Column("resource", sa.String(length=2048), nullable=False),
        sa.Column("key_hash", sa.String(length=71), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("code_hash"),
    )
    op.create_index(
        "ix_oauth_authorization_code_expires_at", "oauth_authorization_code", ["expires_at"]
    )
    op.create_table(
        "oauth_grant",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("client_id", sa.String(length=2048), nullable=False),
        sa.Column("client_name", sa.String(length=200), nullable=False),
        sa.Column("key_hash", sa.String(length=71), nullable=False),
        sa.Column("resource", sa.String(length=2048), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_oauth_grant_expires_at", "oauth_grant", ["expires_at"])
    op.create_table(
        "oauth_token",
        sa.Column("token_hash", sa.String(length=71), nullable=False),
        sa.Column("grant_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('access', 'refresh')", name="ck_oauth_token_kind"),
        sa.ForeignKeyConstraint(["grant_id"], ["oauth_grant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    op.create_index("ix_oauth_token_grant_id", "oauth_token", ["grant_id"])
    op.create_index("ix_oauth_token_expires_at", "oauth_token", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_oauth_token_expires_at", table_name="oauth_token")
    op.drop_index("ix_oauth_token_grant_id", table_name="oauth_token")
    op.drop_table("oauth_token")
    op.drop_index("ix_oauth_grant_expires_at", table_name="oauth_grant")
    op.drop_table("oauth_grant")
    op.drop_index("ix_oauth_authorization_code_expires_at", table_name="oauth_authorization_code")
    op.drop_table("oauth_authorization_code")
