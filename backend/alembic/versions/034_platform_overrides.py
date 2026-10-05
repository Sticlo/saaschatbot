"""excepciones por empresa desde la consola de plataforma (límites, funciones, nota interna)

Revision ID: 034
Revises: 033
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "034"
down_revision = "033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("platform_overrides", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenants", "platform_overrides")
