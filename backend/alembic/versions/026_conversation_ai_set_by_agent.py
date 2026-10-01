"""conversation ai_set_by_agent: la decisión del agente sobre la IA no se sobrescribe

Revision ID: 026
Revises: 025
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "026"
down_revision = "025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("ai_set_by_agent", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("conversations", "ai_set_by_agent")
