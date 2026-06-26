"""Alembic migration: IDs Chatwoot para deduplicación."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("chatwoot_conversation_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_conversations_chatwoot_conversation_id",
        "conversations",
        ["chatwoot_conversation_id"],
        unique=False,
    )
    op.add_column(
        "messages",
        sa.Column("chatwoot_message_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_messages_chatwoot_message_id",
        "messages",
        ["chatwoot_message_id"],
        unique=False,
    )
    op.create_index(
        "uq_messages_tenant_chatwoot_msg",
        "messages",
        ["tenant_id", "chatwoot_message_id"],
        unique=True,
        postgresql_where=sa.text("chatwoot_message_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_messages_tenant_chatwoot_msg", table_name="messages")
    op.drop_index("ix_messages_chatwoot_message_id", table_name="messages")
    op.drop_column("messages", "chatwoot_message_id")
    op.drop_index("ix_conversations_chatwoot_conversation_id", table_name="conversations")
    op.drop_column("conversations", "chatwoot_conversation_id")
