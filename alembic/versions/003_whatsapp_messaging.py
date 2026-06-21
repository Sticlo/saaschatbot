"""whatsapp sessions, conversations and messages

Revision ID: 003
Revises: 002
Create Date: 2025-06-21

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instance_name", sa.String(length=80), nullable=False),
        sa.Column("phone_number", sa.String(length=20), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("qr_base64", sa.Text(), nullable=True),
        sa.Column("qr_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_disconnected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("instance_name"),
        sa.UniqueConstraint("tenant_id"),
    )
    op.create_index("ix_whatsapp_sessions_instance_name", "whatsapp_sessions", ["instance_name"])
    op.create_index("ix_whatsapp_sessions_tenant_id", "whatsapp_sessions", ["tenant_id"])

    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("contact_phone", sa.String(length=20), nullable=False),
        sa.Column("contact_name", sa.String(length=255), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("ai_active", sa.Boolean(), nullable=False),
        sa.Column("bait_sent", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("unread_count", sa.Integer(), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "contact_phone", name="uq_conversations_tenant_phone"),
    )
    op.create_index("ix_conversations_contact_phone", "conversations", ["contact_phone"])
    op.create_index("ix_conversations_tenant_id", "conversations", ["tenant_id"])

    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("body", sa.String(length=4096), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("evolution_message_id", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])
    op.create_index("ix_messages_created_at", "messages", ["created_at"])
    op.create_index("ix_messages_evolution_message_id", "messages", ["evolution_message_id"])
    op.create_index("ix_messages_tenant_id", "messages", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_messages_tenant_id", table_name="messages")
    op.drop_index("ix_messages_evolution_message_id", table_name="messages")
    op.drop_index("ix_messages_created_at", table_name="messages")
    op.drop_index("ix_messages_conversation_id", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_conversations_tenant_id", table_name="conversations")
    op.drop_index("ix_conversations_contact_phone", table_name="conversations")
    op.drop_table("conversations")
    op.drop_index("ix_whatsapp_sessions_tenant_id", table_name="whatsapp_sessions")
    op.drop_index("ix_whatsapp_sessions_instance_name", table_name="whatsapp_sessions")
    op.drop_table("whatsapp_sessions")
