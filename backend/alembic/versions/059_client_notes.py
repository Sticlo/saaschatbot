"""lo que la IA recuerda de cada cliente (nombres, ocasión, gustos) para usarlo con naturalidad

Revision ID: 059
Revises: 058
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "059"
down_revision = "058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("client_notes", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("conversations", "client_notes")
