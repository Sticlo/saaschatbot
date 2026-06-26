"""maps prospect runs and leads

Revision ID: 017
Revises: 016
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "maps_prospect_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("business_description", sa.Text(), nullable=False),
        sa.Column("city", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("ai_plan", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="completed"),
        sa.Column("leads_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_maps_prospect_runs_tenant_id",
        "maps_prospect_runs",
        ["tenant_id"],
    )
    op.create_index(
        "ix_maps_prospect_runs_created_at",
        "maps_prospect_runs",
        ["created_at"],
    )

    op.create_table(
        "maps_prospect_leads",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("maps_prospect_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("phone_e164", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("address", sa.Text(), nullable=False, server_default=""),
        sa.Column("category", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("maps_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("search_query", sa.String(length=255), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_maps_prospect_leads_run_id",
        "maps_prospect_leads",
        ["run_id"],
    )
    op.create_index(
        "ix_maps_prospect_leads_tenant_id",
        "maps_prospect_leads",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_maps_prospect_leads_tenant_id", table_name="maps_prospect_leads")
    op.drop_index("ix_maps_prospect_leads_run_id", table_name="maps_prospect_leads")
    op.drop_table("maps_prospect_leads")
    op.drop_index("ix_maps_prospect_runs_created_at", table_name="maps_prospect_runs")
    op.drop_index("ix_maps_prospect_runs_tenant_id", table_name="maps_prospect_runs")
    op.drop_table("maps_prospect_runs")
