"""Payment checkouts for Wompi

Revision ID: 023
Revises: 022
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "023"
down_revision = "022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payment_checkouts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("plan_id", sa.UUID(), nullable=False),
        sa.Column("reference", sa.String(length=64), nullable=False),
        sa.Column("amount_in_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="COP"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("customer_email", sa.String(length=255), nullable=False),
        sa.Column("wompi_transaction_id", sa.String(length=64), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("reference"),
    )
    op.create_index("ix_payment_checkouts_tenant_id", "payment_checkouts", ["tenant_id"])
    op.create_index("ix_payment_checkouts_plan_id", "payment_checkouts", ["plan_id"])
    op.create_index("ix_payment_checkouts_reference", "payment_checkouts", ["reference"])


def downgrade() -> None:
    op.drop_index("ix_payment_checkouts_reference", table_name="payment_checkouts")
    op.drop_index("ix_payment_checkouts_plan_id", table_name="payment_checkouts")
    op.drop_index("ix_payment_checkouts_tenant_id", table_name="payment_checkouts")
    op.drop_table("payment_checkouts")
