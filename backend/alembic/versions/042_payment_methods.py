"""medios de pago del negocio: con ellos la IA cierra la venta sola

Revision ID: 042
Revises: 041
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "042"
down_revision = "041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenant_profiles", sa.Column("payment_methods", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("tenant_profiles", "payment_methods")
