"""Initial DevBox identity and workspace metadata schema."""

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "principals",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("principal_type", sa.String(length=20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("principal_id", sa.String(length=36), sa.ForeignKey("principals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("key_prefix", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "repositories",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("normalized_url", sa.String(length=500), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False, server_default="git"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("normalized_url", name="uq_repositories_normalized_url"),
    )
    op.create_table(
        "workspaces",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("owner_id", sa.String(length=36), sa.ForeignKey("principals.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("repository_id", sa.String(length=36), sa.ForeignKey("repositories.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("branch", sa.String(length=255), nullable=False, server_default="main"),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="registered"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_workspaces_owner_created", "workspaces", ["owner_id", "created_at"])
    op.create_index("ix_workspaces_repository", "workspaces", ["repository_id"])
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("actor_id", sa.String(length=36), sa.ForeignKey("principals.id", ondelete="SET NULL")),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("target_type", sa.String(length=50), nullable=False),
        sa.Column("target_id", sa.String(length=64)),
        sa.Column("result", sa.String(length=30), nullable=False, server_default="success"),
        sa.Column("request_id", sa.String(length=64)),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("message", sa.Text()),
    )
    op.create_index("ix_audit_events_created", "audit_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_events_created", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_workspaces_repository", table_name="workspaces")
    op.drop_index("ix_workspaces_owner_created", table_name="workspaces")
    op.drop_table("workspaces")
    op.drop_table("repositories")
    op.drop_table("api_keys")
    op.drop_table("principals")
