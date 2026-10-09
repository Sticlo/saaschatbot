"""resultados del negocio: quién agendó cada cita y valor promedio del servicio

Las citas creadas desde el panel siempre dejaron un audit log `appointments.created`;
las demás las agendó la IA.

Revision ID: 039
Revises: 038
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "039"
down_revision = "038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "appointments",
        sa.Column("source", sa.String(10), nullable=False, server_default="panel"),
    )
    op.execute(
        """
        UPDATE appointments a
        SET source = 'ai'
        WHERE NOT EXISTS (
            SELECT 1 FROM audit_logs l
            WHERE l.action = 'appointments.created'
              AND l.details ->> 'appointment_id' = a.id::text
        )
        """
    )
    op.add_column("tenant_profiles", sa.Column("avg_ticket_cop", sa.Integer(), nullable=True))
    op.add_column("tenant_profiles", sa.Column("results_report_sent_for", sa.String(7), nullable=True))


def downgrade() -> None:
    op.drop_column("tenant_profiles", "results_report_sent_for")
    op.drop_column("tenant_profiles", "avg_ticket_cop")
    op.drop_column("appointments", "source")
