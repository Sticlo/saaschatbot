"""pausa del horario (p. ej. almuerzo de 13:00 a 14:00): no se agendan citas

Revision ID: 057
Revises: 056
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "057"
down_revision = "056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenant_profiles", sa.Column("schedule_break_start", sa.String(5), nullable=True))
    op.add_column("tenant_profiles", sa.Column("schedule_break_end", sa.String(5), nullable=True))


def downgrade() -> None:
    op.drop_column("tenant_profiles", "schedule_break_end")
    op.drop_column("tenant_profiles", "schedule_break_start")
