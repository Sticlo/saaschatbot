"""pedidos que cierra la IA: esperando pago, listos para despachar y despachados

Revision ID: 048
Revises: 047
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "048"
down_revision = "047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "orders",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "conversation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("contact_name", sa.String(255), nullable=False, server_default=""),
        sa.Column("contact_phone", sa.String(64), nullable=False, server_default=""),
        sa.Column("items", sa.Text(), nullable=False),
        sa.Column("total", sa.String(300), nullable=False, server_default=""),
        sa.Column("customer", sa.String(300), nullable=False, server_default=""),
        sa.Column("delivery", sa.String(300), nullable=False, server_default=""),
        sa.Column("payment", sa.String(300), nullable=False, server_default=""),
        sa.Column("payment_proof", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("tracking", sa.String(120), nullable=False, server_default=""),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_orders_tenant_status", "orders", ["tenant_id", "status"])
    op.create_index("ix_orders_conversation_id", "orders", ["conversation_id"])


def downgrade() -> None:
    op.drop_index("ix_orders_conversation_id", table_name="orders")
    op.drop_index("ix_orders_tenant_status", table_name="orders")
    op.drop_table("orders")
