"""recordatorio de la cita por WhatsApp 2 horas antes

Revision ID: 050
Revises: 049
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "050"
down_revision = "049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("appointments", sa.Column("reminder_sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "tenant_profiles",
        sa.Column("appointment_reminders", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("tenant_profiles", "appointment_reminders")
    op.drop_column("appointments", "reminder_sent_at")
