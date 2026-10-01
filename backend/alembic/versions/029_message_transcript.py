"""messages.transcript: transcripción de notas de voz para que la IA las entienda

Revision ID: 029
Revises: 028
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "029"
down_revision = "028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("transcript", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "transcript")
