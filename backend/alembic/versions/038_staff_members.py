"""agenda por empleado (barberos, estilistas, profesionales)

Los negocios sin equipo registrado siguen con una sola agenda (staff_id NULL).

Revision ID: 038
Revises: 037
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "038"
down_revision = "037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "staff_members",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("phone", sa.String(32), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("work_days", JSONB, nullable=False, server_default=sa.text("'[0,1,2,3,4,5]'::jsonb")),
        sa.Column("start_time", sa.String(5), nullable=True),
        sa.Column("end_time", sa.String(5), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_staff_members_tenant_id", "staff_members", ["tenant_id"])
    op.add_column(
        "appointments",
        sa.Column(
            "staff_id",
            UUID(as_uuid=True),
            sa.ForeignKey("staff_members.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_appointments_staff_id", "appointments", ["staff_id"])
    op.add_column(
        "tenant_profiles",
        sa.Column("staff_label", sa.String(40), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("tenant_profiles", "staff_label")
    op.drop_index("ix_appointments_staff_id", table_name="appointments")
    op.drop_column("appointments", "staff_id")
    op.drop_index("ix_staff_members_tenant_id", table_name="staff_members")
    op.drop_table("staff_members")
