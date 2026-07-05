"""appointments and schedule settings

Revision ID: 024
Revises: 023
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "024"
down_revision = "023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_profiles",
        sa.Column("schedule_open_time", sa.String(length=5), nullable=False, server_default="08:00"),
    )
    op.add_column(
        "tenant_profiles",
        sa.Column("schedule_close_time", sa.String(length=5), nullable=False, server_default="18:00"),
    )
    op.add_column(
        "tenant_profiles",
        sa.Column("schedule_slot_minutes", sa.Integer(), nullable=False, server_default="60"),
    )

    op.create_table(
        "appointments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("client_name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("client_phone", sa.String(length=64), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_appointments_tenant_id", "appointments", ["tenant_id"])
    op.create_index("ix_appointments_starts_at", "appointments", ["starts_at"])
    op.create_index("ix_appointments_tenant_starts", "appointments", ["tenant_id", "starts_at"])


def downgrade() -> None:
    op.drop_index("ix_appointments_tenant_starts", table_name="appointments")
    op.drop_index("ix_appointments_starts_at", table_name="appointments")
    op.drop_index("ix_appointments_tenant_id", table_name="appointments")
    op.drop_table("appointments")
    op.drop_column("tenant_profiles", "schedule_slot_minutes")
    op.drop_column("tenant_profiles", "schedule_close_time")
    op.drop_column("tenant_profiles", "schedule_open_time")
