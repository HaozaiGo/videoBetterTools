"""add task progress fields

Revision ID: 0003_task_progress
Revises: 0002_auth_users
Create Date: 2026-06-03
"""

from alembic import op
import sqlalchemy as sa

revision = "0003_task_progress"
down_revision = "0002_auth_users"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("progress_percent", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("tasks", sa.Column("progress_stage", sa.String(length=160), nullable=False, server_default=""))
    op.alter_column("tasks", "progress_percent", server_default=None)
    op.alter_column("tasks", "progress_stage", server_default=None)


def downgrade() -> None:
    op.drop_column("tasks", "progress_stage")
    op.drop_column("tasks", "progress_percent")
