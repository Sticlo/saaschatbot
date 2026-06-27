"""OAuth columns on users (Google, GitHub)

Revision ID: 022
Revises: 021
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "022"
down_revision = "021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("oauth_provider", sa.String(length=20), nullable=True))
    op.add_column("users", sa.Column("oauth_subject", sa.String(length=255), nullable=True))
    op.alter_column("users", "hashed_password", existing_type=sa.String(length=255), nullable=True)
    op.create_index("ix_users_oauth_provider", "users", ["oauth_provider"], unique=False)
    op.create_index("ix_users_oauth_subject", "users", ["oauth_subject"], unique=False)
    op.create_unique_constraint(
        "uq_users_oauth_provider_subject",
        "users",
        ["oauth_provider", "oauth_subject"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_users_oauth_provider_subject", "users", type_="unique")
    op.drop_index("ix_users_oauth_subject", table_name="users")
    op.drop_index("ix_users_oauth_provider", table_name="users")
    op.alter_column("users", "hashed_password", existing_type=sa.String(length=255), nullable=False)
    op.drop_column("users", "oauth_subject")
    op.drop_column("users", "oauth_provider")
