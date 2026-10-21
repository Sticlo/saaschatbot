"""calendarios de disponibilidad por alojamiento o espacio (iCal)

Revision ID: 055
Revises: 054
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "055"
down_revision = "054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenant_profiles", sa.Column("availability_calendars", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("tenant_profiles", "availability_calendars")
