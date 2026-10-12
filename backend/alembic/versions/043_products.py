"""productos del negocio: precio, cuántos quedan y detalles para que la IA venda sola

Revision ID: 043
Revises: 042
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "043"
down_revision = "042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "products",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("price_cop", sa.Integer, nullable=True),
        sa.Column("stock", sa.Integer, nullable=True),
        sa.Column("details", sa.Text, nullable=True),
        sa.Column("position", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_products_tenant_id", "products", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_products_tenant_id", table_name="products")
    op.drop_table("products")
