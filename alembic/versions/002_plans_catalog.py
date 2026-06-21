"""plans catalog and subscription plan_id

Revision ID: 002
Revises: 001
Create Date: 2025-06-21

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_PLAN_ID = "a0000000-0000-4000-8000-000000000001"


def upgrade() -> None:
    op.create_table(
        "plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("price_cop", sa.Integer(), nullable=False),
        sa.Column("price_usd_cents", sa.Integer(), nullable=True),
        sa.Column("trial_bait_limit", sa.Integer(), nullable=False),
        sa.Column("daily_bait_limit", sa.Integer(), nullable=False),
        sa.Column("max_team_members", sa.Integer(), nullable=False),
        sa.Column("features", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_public", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index("ix_plans_slug", "plans", ["slug"], unique=False)

    op.execute(
        sa.text(
            """
            INSERT INTO plans (
                id, slug, name, description, price_cop, price_usd_cents,
                trial_bait_limit, daily_bait_limit, max_team_members,
                features, is_active, is_public, sort_order
            ) VALUES (
                :id, 'pro', 'Plan Pro',
                'Prospecta por WhatsApp con IA, extractor de Google Maps y panel en tiempo real.',
                80000, 2000, 10, 100, 3,
                '{"ai_on_reply": true, "maps_scraper": true, "realtime_panel": true}'::jsonb,
                true, true, 0
            )
            """
        ).bindparams(id=DEFAULT_PLAN_ID)
    )

    op.add_column(
        "subscriptions",
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE subscriptions SET plan_id = :plan_id WHERE plan_id IS NULL"
        ).bindparams(plan_id=DEFAULT_PLAN_ID)
    )
    op.alter_column("subscriptions", "plan_id", nullable=False)
    op.create_foreign_key(
        "fk_subscriptions_plan_id",
        "subscriptions",
        "plans",
        ["plan_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_subscriptions_plan_id", "subscriptions", ["plan_id"], unique=False)

    op.drop_column("subscriptions", "trial_bait_limit")
    op.drop_column("subscriptions", "paid_daily_bait_limit")


def downgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column("paid_daily_bait_limit", sa.Integer(), nullable=False, server_default="100"),
    )
    op.add_column(
        "subscriptions",
        sa.Column("trial_bait_limit", sa.Integer(), nullable=False, server_default="10"),
    )
    op.drop_constraint("fk_subscriptions_plan_id", "subscriptions", type_="foreignkey")
    op.drop_index("ix_subscriptions_plan_id", table_name="subscriptions")
    op.drop_column("subscriptions", "plan_id")
    op.drop_index("ix_plans_slug", table_name="plans")
    op.drop_table("plans")
