"""add auth sessions, admin/employee roles and integer audit entity ids

- user_sessions: server-side login sessions (token hashes only)
- users.must_change_password
- roles limited to 'admin' and 'employee'; existing 'staff' users become
  'employee' (owner decision). Any other existing role makes the upgrade fail
  on the check constraint, so it is never silently changed.
- emails normalised to trimmed lower case, with a unique index on
  lower(email). Fails if two existing emails differ only by case.
- audit_logs.entity_id: VARCHAR -> INTEGER (integer IDs everywhere). Fails if
  an existing value is not an integer, rather than discarding it.

Revision ID: a3d81f5c6e20
Revises: 7c4e2a91d5b3
Create Date: 2026-11-19 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3d81f5c6e20"
down_revision: str | Sequence[str] | None = "7c4e2a91d5b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_user_sessions_token_hash"),
        "user_sessions",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        op.f("ix_user_sessions_user_id"), "user_sessions", ["user_id"], unique=False
    )

    op.add_column(
        "users",
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.execute("UPDATE users SET role = 'employee' WHERE role = 'staff'")
    op.create_check_constraint(
        "ck_users_role", "users", "role IN ('admin', 'employee')"
    )

    op.execute("UPDATE users SET email = lower(trim(email))")
    op.create_index(
        "uq_users_email_lower", "users", [sa.text("lower(email)")], unique=True
    )

    op.alter_column(
        "audit_logs",
        "entity_id",
        existing_type=sa.String(length=100),
        type_=sa.Integer(),
        existing_nullable=True,
        postgresql_using="entity_id::integer",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column(
        "audit_logs",
        "entity_id",
        existing_type=sa.Integer(),
        type_=sa.String(length=100),
        existing_nullable=True,
        postgresql_using="entity_id::text",
    )
    op.drop_index("uq_users_email_lower", table_name="users")
    op.drop_constraint("ck_users_role", "users", type_="check")
    # Lossy by design: the original schema only knew the 'staff' default.
    op.execute("UPDATE users SET role = 'staff' WHERE role = 'employee'")
    op.drop_column("users", "must_change_password")
    op.drop_index(op.f("ix_user_sessions_user_id"), table_name="user_sessions")
    op.drop_index(op.f("ix_user_sessions_token_hash"), table_name="user_sessions")
    op.drop_table("user_sessions")
