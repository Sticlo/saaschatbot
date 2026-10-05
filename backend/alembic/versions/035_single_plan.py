"""un solo plan: $200.000 con todo incluido (se elimina el de $120.000)

Se conserva la fila `pro` (todas las cuentas apuntan a ella) con lo que tenía Premium,
y lo que estuviera en Premium pasa a esa fila.

Revision ID: 035
Revises: 034
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "035"
down_revision = "034"
branch_labels = None
depends_on = None

PLAN_ID = "a0000000-0000-4000-8000-000000000001"
PREMIUM_ID = "a0000000-0000-4000-8000-000000000002"

FEATURES = {
    "ai_on_reply": True,
    "realtime_panel": True,
    "maps_scraper": True,
    "priority_support": True,
    "ai_daily_replies": 0,
    "ai_daily_classifications": 0,
}

OLD_PRO_FEATURES = {
    "ai_on_reply": True,
    "realtime_panel": True,
    "maps_scraper": False,
    "priority_support": False,
    "ai_daily_replies": 400,
    "ai_daily_classifications": 0,
}


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE plans SET
                name = 'Plan Pro',
                description = 'Todo Omitel incluido: IA sin límite que atiende tu WhatsApp, catálogo, citas, equipo de hasta 5 personas y soporte prioritario.',
                price_cop = 200000,
                price_usd_cents = 5000,
                daily_bait_limit = 100,
                max_team_members = 5,
                features = CAST(:features AS jsonb),
                is_active = true,
                is_public = true,
                sort_order = 0
            WHERE id = CAST(:id AS uuid)
            """
        ).bindparams(id=PLAN_ID, features=json.dumps(FEATURES))
    )
    for table in ("subscriptions", "payment_checkouts"):
        op.execute(
            sa.text(
                f"UPDATE {table} SET plan_id = CAST(:keep AS uuid) WHERE plan_id = CAST(:old AS uuid)"
            ).bindparams(keep=PLAN_ID, old=PREMIUM_ID)
        )
    op.execute(sa.text("DELETE FROM plans WHERE id = CAST(:old AS uuid)").bindparams(old=PREMIUM_ID))
    op.execute(sa.text("UPDATE tenants SET daily_bait_limit = 100 WHERE plan = 'paid'"))


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE plans SET
                description = 'IA que saluda, responde dudas y te avisa quién quiere comprar. Ideal para negocios con WhatsApp activo.',
                price_cop = 120000,
                price_usd_cents = 3000,
                daily_bait_limit = 50,
                max_team_members = 2,
                features = CAST(:features AS jsonb)
            WHERE id = CAST(:id AS uuid)
            """
        ).bindparams(id=PLAN_ID, features=json.dumps(OLD_PRO_FEATURES))
    )
    op.execute(
        sa.text(
            """
            INSERT INTO plans (
                id, slug, name, description, price_cop, price_usd_cents,
                trial_bait_limit, daily_bait_limit, max_team_members,
                features, is_active, is_public, sort_order
            )
            SELECT CAST(:id AS uuid), 'premium', 'Plan Premium',
                'Todo lo del Pro con IA ilimitada, más equipo y soporte prioritario. Para alto volumen en WhatsApp.',
                200000, 5000, 10, 100, 5, CAST(:features AS jsonb), true, true, 1
            WHERE NOT EXISTS (SELECT 1 FROM plans WHERE slug = 'premium')
            """
        ).bindparams(id=PREMIUM_ID, features=json.dumps(FEATURES))
    )
