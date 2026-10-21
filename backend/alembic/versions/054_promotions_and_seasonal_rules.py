"""promociones y reglas de temporada en «Personalizar IA»

Revision ID: 054
Revises: 053
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "054"
down_revision = "053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenant_profiles", sa.Column("promotions", sa.Text(), nullable=True))
    op.add_column("tenant_profiles", sa.Column("seasonal_rules", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("tenant_profiles", "seasonal_rules")
    op.drop_column("tenant_profiles", "promotions")
