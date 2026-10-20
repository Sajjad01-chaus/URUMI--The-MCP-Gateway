"""record the managed release kind on upstream servers

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-20
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("upstream_servers", sa.Column("managed_kind", sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column("upstream_servers", "managed_kind")
