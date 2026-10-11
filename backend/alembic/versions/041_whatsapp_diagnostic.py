"""diagnóstico del WhatsApp al vincular

Solo números (clientes, sin respuesta, tiempos, temas): ni textos ni teléfonos.

Revision ID: 041
Revises: 040
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "041"
down_revision = "040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenant_profiles", sa.Column("whatsapp_diagnostic", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("tenant_profiles", "whatsapp_diagnostic")
