"""Index on conversations.contact_jid for fast JID lookups

Revision ID: 012
Revises: 011
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect

revision: str = "012"
down_revision: Union[str, None] = "011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _index_exists(name: str) -> bool:
    bind = op.get_bind()
    return name in [idx["name"] for idx in inspect(bind).get_indexes("conversations")]


def upgrade() -> None:
    if not _index_exists("ix_conversations_jid_lookup"):
        op.create_index(
            "ix_conversations_jid_lookup",
            "conversations",
            ["tenant_id", "whatsapp_connection_id", "contact_jid"],
            postgresql_where="contact_jid IS NOT NULL",
        )


def downgrade() -> None:
    if _index_exists("ix_conversations_jid_lookup"):
        op.drop_index("ix_conversations_jid_lookup", table_name="conversations")
