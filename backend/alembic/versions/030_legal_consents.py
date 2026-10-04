"""legal_consents: evidencia de aceptación de términos y política de datos

Revision ID: 030
Revises: 029
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "030"
down_revision = "029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "legal_consents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("terms_version", sa.String(20), nullable=False),
        sa.Column("privacy_version", sa.String(20), nullable=False),
        sa.Column("marketing_opt_in", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("method", sa.String(40), nullable=False),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.String(400), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_legal_consents_tenant_id", "legal_consents", ["tenant_id"])
    op.create_index("ix_legal_consents_user_id", "legal_consents", ["user_id"])
    op.create_index("ix_legal_consents_email", "legal_consents", ["email"])


def downgrade() -> None:
    op.drop_table("legal_consents")
