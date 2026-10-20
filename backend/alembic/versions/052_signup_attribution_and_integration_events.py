"""de dónde llegó cada registro (UTM, lead) y eventos pendientes para siochat

Revision ID: 052
Revises: 051
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "052"
down_revision = "051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("lead_phone", sa.String(15), nullable=True))
    op.add_column("tenants", sa.Column("signup_attribution", JSONB, nullable=True))
    op.create_index("ix_tenants_lead_phone", "tenants", ["lead_phone"])

    op.create_table(
        "integration_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event", sa.String(40), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_integration_events_tenant_id", "integration_events", ["tenant_id"])
    op.create_index(
        "ix_integration_events_pending",
        "integration_events",
        ["next_attempt_at"],
        postgresql_where=sa.text("delivered_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_integration_events_pending", table_name="integration_events")
    op.drop_index("ix_integration_events_tenant_id", table_name="integration_events")
    op.drop_table("integration_events")
    op.drop_index("ix_tenants_lead_phone", table_name="tenants")
    op.drop_column("tenants", "signup_attribution")
    op.drop_column("tenants", "lead_phone")
