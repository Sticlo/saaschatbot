"""plans Pro $120k and Premium $200k

Revision ID: 021
Revises: 020
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "021"
down_revision = "020"
branch_labels = None
depends_on = None

PRO_ID = "a0000000-0000-4000-8000-000000000001"
PREMIUM_ID = "a0000000-0000-4000-8000-000000000002"

PRO_FEATURES = {
    "ai_on_reply": True,
    "realtime_panel": True,
    "maps_scraper": False,
    "priority_support": False,
    "ai_daily_replies": 400,
    "ai_daily_classifications": 0,
}

PREMIUM_FEATURES = {
    "ai_on_reply": True,
    "realtime_panel": True,
    "maps_scraper": True,
    "priority_support": True,
    "ai_daily_replies": 0,
    "ai_daily_classifications": 0,
}


def upgrade() -> None:
    pro_features = json.dumps(PRO_FEATURES)
    premium_features = json.dumps(PREMIUM_FEATURES)

    op.execute(
        sa.text(
            """
            UPDATE plans SET
                name = 'Plan Pro',
                description = 'IA que saluda, responde dudas y te avisa quién quiere comprar. Ideal para negocios con WhatsApp activo.',
                price_cop = 120000,
                price_usd_cents = 3000,
                trial_bait_limit = 10,
                daily_bait_limit = 50,
                max_team_members = 2,
                features = CAST(:features AS jsonb),
                is_active = true,
                is_public = true,
                sort_order = 0
            WHERE slug = 'pro'
            """
        ).bindparams(features=pro_features)
    )

    op.execute(
        sa.text(
            """
            INSERT INTO plans (
                id, slug, name, description, price_cop, price_usd_cents,
                trial_bait_limit, daily_bait_limit, max_team_members,
                features, is_active, is_public, sort_order
            )
            SELECT
                CAST(:id AS uuid),
                'premium',
                'Plan Premium',
                'Todo lo del Pro con IA ilimitada, más equipo y soporte prioritario. Para alto volumen en WhatsApp.',
                200000,
                5000,
                10,
                100,
                5,
                CAST(:features AS jsonb),
                true,
                true,
                1
            WHERE NOT EXISTS (SELECT 1 FROM plans WHERE slug = 'premium')
            """
        ).bindparams(id=PREMIUM_ID, features=premium_features)
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM plans WHERE slug = 'premium'"))
    op.execute(
        sa.text(
            """
            UPDATE plans SET
                name = 'Plan Pro',
                description = 'Prospecta por WhatsApp con IA, extractor de Google Maps y panel en tiempo real.',
                price_cop = 80000,
                price_usd_cents = 2000,
                daily_bait_limit = 100,
                max_team_members = 3,
                features = '{"ai_on_reply": true, "maps_scraper": true, "realtime_panel": true}'::jsonb,
                sort_order = 0
            WHERE slug = 'pro'
            """
        )
    )
