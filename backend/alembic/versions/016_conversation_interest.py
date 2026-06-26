"""conversation interest_status for lead tagging

Revision ID: 016
Revises: 015
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("interest_status", sa.String(length=20), nullable=True),
    )
    op.create_index("ix_conversations_interest_status", "conversations", ["interest_status"])


def downgrade() -> None:
    op.drop_index("ix_conversations_interest_status", table_name="conversations")
    op.drop_column("conversations", "interest_status")
