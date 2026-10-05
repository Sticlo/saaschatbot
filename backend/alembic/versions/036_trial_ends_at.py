"""prueba gratis con fecha de fin

Las cuentas que ya estaban en prueba arrancan sus 3 días desde esta migración,
para no cortarle el servicio a nadie de golpe.

Revision ID: 036
Revises: 035
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "036"
down_revision = "035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("subscriptions", sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE subscriptions SET trial_ends_at = now() + interval '3 days' WHERE status = 'trial'")


def downgrade() -> None:
    op.drop_column("subscriptions", "trial_ends_at")
