"""Ampliar body de mensajes a TEXT (sin límite de 4096)

Revision ID: 009
Revises: 008
"""

from alembic import op
import sqlalchemy as sa

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "messages",
        "body",
        existing_type=sa.String(4096),
        type_=sa.Text(),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "messages",
        "body",
        existing_type=sa.Text(),
        type_=sa.String(4096),
        existing_nullable=False,
    )
