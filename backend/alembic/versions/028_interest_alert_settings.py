"""tenant_profiles alert_phone/alert_threshold: aviso por WhatsApp al dueño cuando se acumulan interesados sin responder

Revision ID: 028
Revises: 027
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenant_profiles", sa.Column("alert_phone", sa.String(32), nullable=True))
    op.add_column(
        "tenant_profiles",
        sa.Column("alert_threshold", sa.Integer(), nullable=False, server_default="10"),
    )


def downgrade() -> None:
    op.drop_column("tenant_profiles", "alert_threshold")
    op.drop_column("tenant_profiles", "alert_phone")
