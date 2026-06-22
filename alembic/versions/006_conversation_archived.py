"""conversation is_archived + fix lid placeholder names

Revision ID: 006
Revises: 005
"""

from alembic import op
import sqlalchemy as sa

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_conversations_is_archived", "conversations", ["is_archived"])

    op.execute(
        """
        UPDATE conversations
        SET contact_name = 'Contacto'
        WHERE contact_name LIKE 'lid:%'
           OR contact_name = contact_phone
        """
    )


def downgrade() -> None:
    op.drop_index("ix_conversations_is_archived", table_name="conversations")
    op.drop_column("conversations", "is_archived")
