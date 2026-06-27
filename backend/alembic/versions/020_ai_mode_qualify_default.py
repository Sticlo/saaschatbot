"""default ai_mode qualify

Revision ID: 020
Revises: 019
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "020"
down_revision = "019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "tenant_profiles",
        "ai_mode",
        server_default="qualify",
        existing_type=sa.String(length=20),
        existing_nullable=False,
    )
    op.execute(
        "UPDATE tenant_profiles SET ai_mode = 'qualify' WHERE ai_mode = 'classify_only'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE tenant_profiles SET ai_mode = 'classify_only' WHERE ai_mode = 'qualify'"
    )
    op.alter_column(
        "tenant_profiles",
        "ai_mode",
        server_default="classify_only",
        existing_type=sa.String(length=20),
        existing_nullable=False,
    )
