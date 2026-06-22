"""whatsapp session connection_started_at

Revision ID: 007
Revises: 006
"""

from alembic import op
import sqlalchemy as sa

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "whatsapp_sessions",
        sa.Column("connection_started_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("whatsapp_sessions", "connection_started_at")
