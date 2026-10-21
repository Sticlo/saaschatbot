"""conexión del sitio web del negocio (MCP con autorización del dueño)

Revision ID: 056
Revises: 055
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "056"
down_revision = "055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenant_profiles", sa.Column("site_connection", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("tenant_profiles", "site_connection")
