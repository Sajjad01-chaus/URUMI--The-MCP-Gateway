"""managed release desired state

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-19
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "managed_releases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("slug", sa.String(length=20), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column("namespace", sa.String(length=63), nullable=False),
        sa.Column("release", sa.String(length=53), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("desired", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("status_reason", sa.Text(), nullable=False),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.Column("secrets_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("server_id", sa.Uuid(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=100), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('store', 'hosted_mcp')", name="ck_managed_releases_kind"),
        sa.CheckConstraint("desired IN ('present', 'absent')", name="ck_managed_releases_desired"),
        sa.CheckConstraint(
            "status IN ('provisioning', 'ready', 'failed', 'deleting', 'deleted')",
            name="ck_managed_releases_status",
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["server_id"], ["upstream_servers.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_managed_releases_owner_id", "managed_releases", ["owner_id"])
    op.create_index("ix_managed_releases_server_id", "managed_releases", ["server_id"])
    op.create_index("ix_managed_releases_lease_until", "managed_releases", ["lease_until"])
    op.create_index(
        "ix_managed_releases_work", "managed_releases", ["status", "lease_until", "created_at"]
    )
    op.create_index(
        "uq_managed_releases_active_slug",
        "managed_releases",
        ["slug"],
        unique=True,
        postgresql_where=sa.text("status <> 'deleted'"),
        sqlite_where=sa.text("status <> 'deleted'"),
    )


def downgrade() -> None:
    op.drop_table("managed_releases")
