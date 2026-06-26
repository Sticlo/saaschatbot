"""bait templates and rich outbound payloads

Revision ID: 014
Revises: 013
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bait_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("image_path", sa.String(length=512), nullable=True),
        sa.Column("buttons", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("button_title", sa.String(length=120), nullable=True),
        sa.Column("button_footer", sa.String(length=255), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
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
    )
    op.create_index("ix_bait_templates_tenant_id", "bait_templates", ["tenant_id"])

    op.add_column(
        "campaigns",
        sa.Column("bait_template_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_campaigns_bait_template_id",
        "campaigns",
        "bait_templates",
        ["bait_template_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column(
        "send_queue",
        sa.Column("message_extras", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("send_queue", "message_extras")
    op.drop_constraint("fk_campaigns_bait_template_id", "campaigns", type_="foreignkey")
    op.drop_column("campaigns", "bait_template_id")
    op.drop_index("ix_bait_templates_tenant_id", table_name="bait_templates")
    op.drop_table("bait_templates")
