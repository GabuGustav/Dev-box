"""Add executor and persistent volume references to workspaces."""

import sqlalchemy as sa
from alembic import op

revision = "0002_workspace_executor"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workspaces", sa.Column("executor_id", sa.String(length=128), nullable=True))
    op.add_column("workspaces", sa.Column("volume_name", sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column("workspaces", "volume_name")
    op.drop_column("workspaces", "executor_id")
