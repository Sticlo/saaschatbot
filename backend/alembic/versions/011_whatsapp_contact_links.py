"""whatsapp contact links (lid <-> phone)

Revision ID: 011
Revises: 010
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision: str = "011"
down_revision: Union[str, None] = "010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    return name in inspect(bind).get_table_names()


def upgrade() -> None:
    if not _table_exists("whatsapp_contact_links"):
        op.create_table(
            "whatsapp_contact_links",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "tenant_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("whatsapp_connection_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("lid_jid", sa.String(128), nullable=False),
            sa.Column("phone_e164", sa.String(32), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.UniqueConstraint(
                "tenant_id",
                "whatsapp_connection_id",
                "lid_jid",
                name="uq_wa_contact_links_lid",
            ),
            sa.UniqueConstraint(
                "tenant_id",
                "whatsapp_connection_id",
                "phone_e164",
                name="uq_wa_contact_links_phone",
            ),
        )
        op.create_index(
            "ix_whatsapp_contact_links_tenant_id",
            "whatsapp_contact_links",
            ["tenant_id"],
        )
        op.create_index(
            "ix_whatsapp_contact_links_connection",
            "whatsapp_contact_links",
            ["whatsapp_connection_id"],
        )

    # Aprender enlaces ya presentes en conversaciones (@lid + teléfono real).
    op.execute(
        """
        INSERT INTO whatsapp_contact_links (
            id, tenant_id, whatsapp_connection_id, lid_jid, phone_e164, created_at, updated_at
        )
        SELECT
            gen_random_uuid(),
            c.tenant_id,
            c.whatsapp_connection_id,
            c.contact_jid,
            c.contact_phone,
            now(),
            now()
        FROM conversations c
        WHERE c.whatsapp_connection_id IS NOT NULL
          AND c.contact_jid LIKE '%@lid'
          AND c.contact_phone LIKE '+%'
          AND length(regexp_replace(c.contact_phone, '\\D', '', 'g')) BETWEEN 10 AND 13
        ON CONFLICT ON CONSTRAINT uq_wa_contact_links_lid DO NOTHING
        """
    )


def downgrade() -> None:
    if _table_exists("whatsapp_contact_links"):
        op.drop_table("whatsapp_contact_links")
