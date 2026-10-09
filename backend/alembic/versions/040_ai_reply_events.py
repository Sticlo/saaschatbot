"""respuestas de la IA que sobreviven a desvincular WhatsApp

Desvincular borra chats y mensajes; los resultados del negocio se cuentan desde aquí.
Se llena con las respuestas que todavía existen.

Revision ID: 040
Revises: 039
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "040"
down_revision = "039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_reply_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("contact_key", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_ai_reply_events_tenant_created", "ai_reply_events", ["tenant_id", "created_at"])
    op.execute(
        """
        INSERT INTO ai_reply_events (id, tenant_id, contact_key, created_at)
        SELECT m.id, m.tenant_id, LEFT(COALESCE(NULLIF(c.contact_phone, ''), c.id::text), 64), m.created_at
        FROM messages m
        JOIN conversations c ON c.id = m.conversation_id
        WHERE m.source = 'bot' AND m.direction = 'out'
        """
    )


def downgrade() -> None:
    op.drop_index("ix_ai_reply_events_tenant_created", table_name="ai_reply_events")
    op.drop_table("ai_reply_events")
