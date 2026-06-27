"""tenant business config columns + backfill missing profiles

Revision ID: 018
Revises: 017
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenant_profiles", sa.Column("industry", sa.String(length=255), nullable=True))
    op.add_column("tenant_profiles", sa.Column("products_services", sa.Text(), nullable=True))
    op.add_column("tenant_profiles", sa.Column("target_customer", sa.String(length=500), nullable=True))
    op.add_column("tenant_profiles", sa.Column("price_range", sa.String(length=255), nullable=True))
    op.add_column("tenant_profiles", sa.Column("location_hours", sa.String(length=500), nullable=True))
    op.add_column("tenant_profiles", sa.Column("tone", sa.String(length=120), nullable=True))
    op.add_column("tenant_profiles", sa.Column("restrictions", sa.Text(), nullable=True))
    op.add_column("tenant_profiles", sa.Column("maps_prospect_business", sa.Text(), nullable=True))
    op.add_column("tenant_profiles", sa.Column("maps_prospect_city", sa.String(length=120), nullable=True))

    # Perfiles faltantes (tenants viejos sin fila en tenant_profiles)
    op.execute(
        """
        INSERT INTO tenant_profiles (id, tenant_id, onboarding_answers, created_at, updated_at)
        SELECT gen_random_uuid(), t.id, '{}'::jsonb, now(), now()
        FROM tenants t
        WHERE NOT EXISTS (
            SELECT 1 FROM tenant_profiles p WHERE p.tenant_id = t.id
        )
        """
    )

    # Migrar JSON legacy → columnas dedicadas
    op.execute(
        """
        UPDATE tenant_profiles
        SET
            industry = NULLIF(onboarding_answers->>'industry', ''),
            products_services = NULLIF(onboarding_answers->>'products_services', ''),
            target_customer = NULLIF(onboarding_answers->>'target_customer', ''),
            price_range = NULLIF(onboarding_answers->>'price_range', ''),
            location_hours = NULLIF(onboarding_answers->>'location_hours', ''),
            tone = NULLIF(onboarding_answers->>'tone', ''),
            restrictions = NULLIF(onboarding_answers->>'restrictions', '')
        WHERE onboarding_answers IS NOT NULL
          AND onboarding_answers != '{}'::jsonb
        """
    )


def downgrade() -> None:
    op.drop_column("tenant_profiles", "maps_prospect_city")
    op.drop_column("tenant_profiles", "maps_prospect_business")
    op.drop_column("tenant_profiles", "restrictions")
    op.drop_column("tenant_profiles", "tone")
    op.drop_column("tenant_profiles", "location_hours")
    op.drop_column("tenant_profiles", "price_range")
    op.drop_column("tenant_profiles", "target_customer")
    op.drop_column("tenant_profiles", "products_services")
    op.drop_column("tenant_profiles", "industry")
