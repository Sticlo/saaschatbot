"""envíos del negocio y preguntas que la IA no supo responder

Revision ID: 044
Revises: 043
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "044"
down_revision = "043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenant_profiles", sa.Column("delivery_info", sa.Text, nullable=True))
    op.create_table(
        "ai_questions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "conversation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("topic", sa.String(160), nullable=False),
        sa.Column("topic_key", sa.String(160), nullable=False),
        sa.Column("question", sa.Text, nullable=True),
        sa.Column("answer", sa.Text, nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("times_asked", sa.Integer, nullable=False, server_default="1"),
        sa.Column("last_asked_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_ai_questions_tenant_status", "ai_questions", ["tenant_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_ai_questions_tenant_status", table_name="ai_questions")
    op.drop_table("ai_questions")
    op.drop_column("tenant_profiles", "delivery_info")
