"""cobro automático con fuentes de pago Wompi y cancelación de suscripciones

Revision ID: 031
Revises: 030
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "031"
down_revision = "030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("subscriptions", sa.Column("auto_renew", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("subscriptions", sa.Column("payment_source_id", sa.String(40), nullable=True))
    op.add_column("subscriptions", sa.Column("payment_method_type", sa.String(20), nullable=True))
    op.add_column("subscriptions", sa.Column("payment_method_label", sa.String(80), nullable=True))
    op.add_column("subscriptions", sa.Column("auto_renew_accepted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "subscriptions",
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("subscriptions", sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("subscriptions", sa.Column("cancel_reason", sa.String(40), nullable=True))
    op.add_column("subscriptions", sa.Column("cancel_feedback", sa.String(500), nullable=True))
    op.add_column(
        "subscriptions",
        sa.Column("renewal_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("subscriptions", sa.Column("next_renewal_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("subscriptions", sa.Column("renewal_reminder_for", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_subscriptions_auto_renew_period_end",
        "subscriptions",
        ["auto_renew", "current_period_end"],
    )

    op.add_column(
        "payment_checkouts",
        sa.Column("kind", sa.String(20), nullable=False, server_default="checkout"),
    )
    op.add_column("payment_checkouts", sa.Column("failure_reason", sa.String(300), nullable=True))


def downgrade() -> None:
    op.drop_column("payment_checkouts", "failure_reason")
    op.drop_column("payment_checkouts", "kind")
    op.drop_index("ix_subscriptions_auto_renew_period_end", table_name="subscriptions")
    for column in (
        "renewal_reminder_for",
        "next_renewal_attempt_at",
        "renewal_attempts",
        "cancel_feedback",
        "cancel_reason",
        "cancelled_at",
        "cancel_at_period_end",
        "auto_renew_accepted_at",
        "payment_method_label",
        "payment_method_type",
        "payment_source_id",
        "auto_renew",
    ):
        op.drop_column("subscriptions", column)
