"""cuentas nuevas: la IA arranca apagada y se enciende sola al completar los datos del negocio

Revision ID: 049
Revises: 048
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "049"
down_revision = "048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("ai_autostart_pending", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("tenants", "ai_autostart_pending")
