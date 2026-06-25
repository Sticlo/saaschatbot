"""conversation contact_jid for WhatsApp LID

Revision ID: 004
Revises: 003
Create Date: 2026-06-21

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "conversations",
        "contact_phone",
        existing_type=sa.String(length=20),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.add_column(
        "conversations",
        sa.Column("contact_jid", sa.String(length=128), nullable=True),
    )
    op.create_index("ix_conversations_contact_jid", "conversations", ["contact_jid"])
    op.execute(
        """
        UPDATE conversations
        SET contact_jid = '71021579251813@lid',
            contact_phone = 'lid:71021579251813'
        WHERE contact_phone LIKE '+710%%' OR contact_phone LIKE '710215%%'
        """
    )


def downgrade() -> None:
    op.drop_index("ix_conversations_contact_jid", table_name="conversations")
    op.drop_column("conversations", "contact_jid")
    op.alter_column(
        "conversations",
        "contact_phone",
        existing_type=sa.String(length=64),
        type_=sa.String(length=20),
        existing_nullable=False,
    )
