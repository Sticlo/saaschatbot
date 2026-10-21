"""respuestas de «Tu negocio» sin tope corto: pasan a texto

Revision ID: 053
Revises: 052
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "053"
down_revision = "052"
branch_labels = None
depends_on = None

_COLUMNS = {
    "industry": 255,
    "target_customer": 500,
    "price_range": 255,
    "location_hours": 500,
    "tone": 120,
}


def upgrade() -> None:
    for column, length in _COLUMNS.items():
        op.alter_column(
            "tenant_profiles", column, type_=sa.Text(), existing_type=sa.String(length), existing_nullable=True
        )


def downgrade() -> None:
    for column, length in _COLUMNS.items():
        op.alter_column(
            "tenant_profiles",
            column,
            type_=sa.String(length),
            existing_type=sa.Text(),
            existing_nullable=True,
            postgresql_using=f"left({column}, {length})",
        )
