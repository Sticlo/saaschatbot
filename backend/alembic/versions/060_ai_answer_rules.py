"""base de respuestas oficiales del negocio: pregunta, formas de preguntarla y respuesta

Revision ID: 060
Revises: 059
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "060"
down_revision = "059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenant_profiles", sa.Column("ai_answer_rules", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("tenant_profiles", "ai_answer_rules")
