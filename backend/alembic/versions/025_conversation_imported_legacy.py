"""conversation imported_legacy flag for personal WhatsApp

Revision ID: 025
Revises: 024
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "025"
down_revision = "024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("imported_legacy", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # Conversaciones ya existentes = agenda personal importada al conectar.
    op.execute("UPDATE conversations SET imported_legacy = true")
    op.execute("UPDATE conversations SET imported_legacy = false WHERE bait_sent = true")


def downgrade() -> None:
    op.drop_column("conversations", "imported_legacy")
