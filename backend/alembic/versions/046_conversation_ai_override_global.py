"""conversation ai_override_global: el dueño prendió la IA en este chat con la IA global apagada

Revision ID: 046
Revises: 045
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "046"
down_revision = "045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("ai_override_global", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("conversations", "ai_override_global")
