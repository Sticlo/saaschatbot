"""seguimiento de la IA al cliente que dejó de responder: cada cuánto (por negocio) y cuándo se le escribió

Revision ID: 047
Revises: 046
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "047"
down_revision = "046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_profiles",
        sa.Column("followup_hours", sa.Integer(), nullable=False, server_default="4"),
    )
    op.add_column(
        "conversations",
        sa.Column("followup_sent_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversations", "followup_sent_at")
    op.drop_column("tenant_profiles", "followup_hours")
