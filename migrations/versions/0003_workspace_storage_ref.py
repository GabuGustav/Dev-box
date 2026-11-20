"""Rename the Docker-specific volume reference to a storage backend reference."""

import sqlalchemy as sa
from alembic import op

revision = "0003_workspace_storage_ref"
down_revision = "0002_workspace_executor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "workspaces",
        "volume_name",
        new_column_name="storage_ref",
        existing_type=sa.String(length=128),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "workspaces",
        "storage_ref",
        new_column_name="volume_name",
        existing_type=sa.String(length=128),
        existing_nullable=True,
    )
