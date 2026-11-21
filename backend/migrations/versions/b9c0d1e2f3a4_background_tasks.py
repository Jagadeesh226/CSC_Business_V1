"""background tasks (Phase 2.2 processing engine)

One table: PostgreSQL holds every task's status, attempts, lease and
result; Redis Streams only wake workers. Includes CHECK constraints on
type, status and limits.

Revision ID: b9c0d1e2f3a4
Revises: a7b8c9d0e1f2
Create Date: 2026-11-21 16:24:30.315164

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b9c0d1e2f3a4"
down_revision: Union[str, Sequence[str], None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "background_tasks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_type", sa.String(length=50), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column(
            "run_after",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("locked_by", sa.String(length=100), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
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
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_background_tasks_status",
        ),
        sa.CheckConstraint(
            "task_type IN ('document_ocr', 'vehicle_expiry_lookup', 'vehicle_mobile_lookup')",
            name="ck_background_tasks_type",
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND max_attempts >= 1 AND timeout_seconds >= 1",
            name="ck_background_tasks_limits",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_background_tasks_due",
        "background_tasks",
        ["status", "run_after"],
        unique=False,
    )
    op.create_index(
        op.f("ix_background_tasks_status"), "background_tasks", ["status"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_background_tasks_status"), table_name="background_tasks")
    op.drop_index("ix_background_tasks_due", table_name="background_tasks")
    op.drop_table("background_tasks")
    # ### end Alembic commands ###
