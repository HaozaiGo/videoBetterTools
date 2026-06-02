"""add auth fields to users

Revision ID: 0002_auth_users
Revises: 0001_initial
Create Date: 2026-06-01
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_auth_users"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("password_hash", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("role", sa.String(length=40), nullable=False, server_default="user"))
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
    op.alter_column("users", "role", server_default=None)


def downgrade() -> None:
    op.drop_column("users", "last_login_at")
    op.drop_column("users", "role")
    op.drop_column("users", "password_hash")
