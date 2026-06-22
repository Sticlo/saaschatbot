"""bound_owner_jid + unique evolution message id

Revision ID: 008
Revises: 007
"""

from alembic import op
import sqlalchemy as sa

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "whatsapp_sessions",
        sa.Column("bound_owner_jid", sa.String(length=128), nullable=True),
    )

    op.execute(
        """
        UPDATE conversations
        SET contact_name = 'Contacto'
        WHERE contact_name LIKE 'lid:%'
           OR contact_phone LIKE 'lid:%' AND contact_name = contact_phone
        """
    )

    op.execute(
        """
        DELETE FROM messages m1
        USING messages m2
        WHERE m1.tenant_id = m2.tenant_id
          AND m1.evolution_message_id = m2.evolution_message_id
          AND m1.evolution_message_id IS NOT NULL
          AND m1.id > m2.id
        """
    )

    op.create_index(
        "uq_messages_tenant_evolution_id",
        "messages",
        ["tenant_id", "evolution_message_id"],
        unique=True,
        postgresql_where=sa.text("evolution_message_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_messages_tenant_evolution_id", table_name="messages")
    op.drop_column("whatsapp_sessions", "bound_owner_jid")
