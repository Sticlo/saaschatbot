"""índices por chat y fecha: último mensaje, último del cliente y ventana de 24 h sin recorrer la tabla

Revision ID: 061
Revises: 060
"""

from __future__ import annotations

from alembic import op

revision = "061"
down_revision = "060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_messages_conversation_created", "messages", ["conversation_id", "created_at"])
    op.create_index("ix_messages_tenant_created", "messages", ["tenant_id", "created_at"])
    op.create_index(
        "ix_conversations_tenant_connection_archived",
        "conversations",
        ["tenant_id", "whatsapp_connection_id", "is_archived"],
    )


def downgrade() -> None:
    op.drop_index("ix_conversations_tenant_connection_archived", table_name="conversations")
    op.drop_index("ix_messages_tenant_created", table_name="messages")
    op.drop_index("ix_messages_conversation_created", table_name="messages")
