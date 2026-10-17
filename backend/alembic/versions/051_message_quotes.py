"""mensajes que responden a otro: id de WhatsApp y texto citado

Revision ID: 051
Revises: 050
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "051"
down_revision = "050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("quoted_evolution_id", sa.String(128), nullable=True))
    op.add_column("messages", sa.Column("quoted_body", sa.String(1000), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "quoted_body")
    op.drop_column("messages", "quoted_evolution_id")
