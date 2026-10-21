"""flujos del negocio: saludo y «cuando pregunten por…» con pasos en orden

Revision ID: 058
Revises: 057
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "058"
down_revision = "057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenant_profiles", sa.Column("ai_flows", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("tenant_profiles", "ai_flows")
