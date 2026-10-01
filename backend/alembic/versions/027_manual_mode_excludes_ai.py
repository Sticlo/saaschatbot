"""modo manual e IA activa son excluyentes

Revision ID: 027
Revises: 026
"""

from __future__ import annotations

from alembic import op

revision = "027"
down_revision = "026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE conversations SET ai_active = false WHERE mode = 'manual' AND ai_active = true")


def downgrade() -> None:
    pass
