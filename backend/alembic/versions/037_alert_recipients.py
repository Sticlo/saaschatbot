"""alertas por WhatsApp a varios números (dueño y equipo)

El número único que ya tenía cada negocio pasa a ser el primero de la lista y sigue
recibiendo todas las alertas.

Revision ID: 037
Revises: 036
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "037"
down_revision = "036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_profiles",
        sa.Column("alert_recipients", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.execute(
        """
        UPDATE tenant_profiles
        SET alert_recipients = jsonb_build_array(
            jsonb_build_object('name', '', 'phone', alert_phone, 'scope', 'all')
        )
        WHERE alert_phone IS NOT NULL AND alert_phone <> ''
        """
    )
    op.drop_column("tenant_profiles", "alert_phone")


def downgrade() -> None:
    op.add_column("tenant_profiles", sa.Column("alert_phone", sa.String(32), nullable=True))
    op.execute(
        """
        UPDATE tenant_profiles
        SET alert_phone = alert_recipients -> 0 ->> 'phone'
        WHERE jsonb_array_length(alert_recipients) > 0
        """
    )
    op.drop_column("tenant_profiles", "alert_recipients")
