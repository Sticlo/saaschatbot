"""outbound leads, exclusions, campaigns, send_queue

Revision ID: 010
Revises: 009
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision: str = "010"
down_revision: Union[str, None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    return name in inspect(bind).get_table_names()


def _index_exists(table: str, index: str) -> bool:
    bind = op.get_bind()
    return index in {idx["name"] for idx in inspect(bind).get_indexes(table)}


def upgrade() -> None:
    if not _table_exists("leads"):
        op.create_table(
            "leads",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("phone_e164", sa.String(length=32), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("source", sa.String(length=50), nullable=False, server_default="manual"),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
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
            sa.UniqueConstraint("tenant_id", "phone_e164", name="uq_leads_tenant_phone"),
        )
    if _table_exists("leads") and not _index_exists("leads", "ix_leads_tenant_id"):
        op.create_index("ix_leads_tenant_id", "leads", ["tenant_id"])
    if _table_exists("leads") and not _index_exists("leads", "ix_leads_tenant_status"):
        op.create_index("ix_leads_tenant_status", "leads", ["tenant_id", "status"])

    if not _table_exists("exclusions"):
        op.create_table(
            "exclusions",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("phone_e164", sa.String(length=32), nullable=False),
            sa.Column("reason", sa.String(length=255), nullable=False, server_default=""),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("tenant_id", "phone_e164", name="uq_exclusions_tenant_phone"),
        )
    if _table_exists("exclusions") and not _index_exists("exclusions", "ix_exclusions_tenant_id"):
        op.create_index("ix_exclusions_tenant_id", "exclusions", ["tenant_id"])

    if not _table_exists("campaigns"):
        op.create_table(
            "campaigns",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False, server_default="Campaña"),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
            sa.Column("message_template", sa.Text(), nullable=True),
            sa.Column("total_queued", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_sent", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_skipped", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_failed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
    if _table_exists("campaigns") and not _index_exists("campaigns", "ix_campaigns_tenant_id"):
        op.create_index("ix_campaigns_tenant_id", "campaigns", ["tenant_id"])

    if not _table_exists("send_queue"):
        op.create_table(
            "send_queue",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("phone_e164", sa.String(length=32), nullable=False),
            sa.Column("contact_name", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("message_body", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
            sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("skip_reason", sa.String(length=255), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
    if _table_exists("send_queue") and not _index_exists(
        "send_queue", "ix_send_queue_tenant_status_sched"
    ):
        op.create_index(
            "ix_send_queue_tenant_status_sched",
            "send_queue",
            ["tenant_id", "status", "scheduled_at"],
        )


def downgrade() -> None:
    if _table_exists("send_queue"):
        if _index_exists("send_queue", "ix_send_queue_tenant_status_sched"):
            op.drop_index("ix_send_queue_tenant_status_sched", table_name="send_queue")
        op.drop_table("send_queue")
    if _table_exists("campaigns"):
        if _index_exists("campaigns", "ix_campaigns_tenant_id"):
            op.drop_index("ix_campaigns_tenant_id", table_name="campaigns")
        op.drop_table("campaigns")
    if _table_exists("exclusions"):
        if _index_exists("exclusions", "ix_exclusions_tenant_id"):
            op.drop_index("ix_exclusions_tenant_id", table_name="exclusions")
        op.drop_table("exclusions")
    if _table_exists("leads"):
        if _index_exists("leads", "ix_leads_tenant_status"):
            op.drop_index("ix_leads_tenant_status", table_name="leads")
        if _index_exists("leads", "ix_leads_tenant_id"):
            op.drop_index("ix_leads_tenant_id", table_name="leads")
        op.drop_table("leads")
