"""whatsapp connection scoped conversations

Revision ID: 005
Revises: 004
Create Date: 2026-06-21

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "whatsapp_sessions",
        sa.Column("active_connection_id", sa.UUID(), nullable=True),
    )
    op.create_index(
        "ix_whatsapp_sessions_active_connection_id",
        "whatsapp_sessions",
        ["active_connection_id"],
    )

    op.add_column(
        "conversations",
        sa.Column("whatsapp_connection_id", sa.UUID(), nullable=True),
    )
    op.create_index(
        "ix_conversations_whatsapp_connection_id",
        "conversations",
        ["whatsapp_connection_id"],
    )

    op.drop_constraint("uq_conversations_tenant_phone", "conversations", type_="unique")
    op.create_unique_constraint(
        "uq_conversations_tenant_phone_connection",
        "conversations",
        ["tenant_id", "contact_phone", "whatsapp_connection_id"],
    )

    op.execute("DELETE FROM conversations WHERE whatsapp_connection_id IS NULL")


def downgrade() -> None:
    op.drop_constraint("uq_conversations_tenant_phone_connection", "conversations", type_="unique")
    op.create_unique_constraint(
        "uq_conversations_tenant_phone",
        "conversations",
        ["tenant_id", "contact_phone"],
    )
    op.drop_index("ix_conversations_whatsapp_connection_id", table_name="conversations")
    op.drop_column("conversations", "whatsapp_connection_id")
    op.drop_index("ix_whatsapp_sessions_active_connection_id", table_name="whatsapp_sessions")
    op.drop_column("whatsapp_sessions", "active_connection_id")
