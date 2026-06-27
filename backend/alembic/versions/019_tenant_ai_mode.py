"""tenant ai_mode: classify_only vs full_reply

Revision ID: 019
Revises: 018
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_profiles",
        sa.Column(
            "ai_mode",
            sa.String(length=20),
            nullable=False,
            server_default="classify_only",
        ),
    )


def downgrade() -> None:
    op.drop_column("tenant_profiles", "ai_mode")
