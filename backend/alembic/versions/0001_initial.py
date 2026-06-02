"""initial platform tables

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-01
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("email", sa.String(length=255), nullable=False, unique=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "wallets",
        sa.Column("user_id", sa.String(length=64), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("credits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("frozen_credits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "assets",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("user_id", sa.String(length=64), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=120), nullable=False),
        sa.Column("storage_key", sa.String(length=500), nullable=False),
        sa.Column("url", sa.String(length=500), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("width", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("height", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("user_id", sa.String(length=64), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("tool_slug", sa.String(length=120), nullable=False, index=True),
        sa.Column("input_asset_id", sa.String(length=64), sa.ForeignKey("assets.id"), nullable=False),
        sa.Column("output_asset_id", sa.String(length=64), sa.ForeignKey("assets.id"), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False, index=True),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("estimated_credits", sa.Integer(), nullable=False),
        sa.Column("frozen_credits", sa.Integer(), nullable=False),
        sa.Column("charged_credits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("provider", sa.String(length=120), nullable=False),
        sa.Column("provider_job_id", sa.String(length=160), nullable=False, unique=True, index=True),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("output_url", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "wallet_ledger",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("user_id", sa.String(length=64), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("type", sa.String(length=40), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("task_id", sa.String(length=64), sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "processed_callbacks",
        sa.Column("callback_id", sa.String(length=255), primary_key=True),
        sa.Column("provider_job_id", sa.String(length=160), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("processed_callbacks")
    op.drop_table("wallet_ledger")
    op.drop_table("tasks")
    op.drop_table("assets")
    op.drop_table("wallets")
    op.drop_table("users")
