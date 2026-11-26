"""worker heartbeats (Phase 5.0)

One row per worker process, refreshed every half minute, so an admin can
see whether background work is running.

Revision ID: c5d8e1f2a3b4
Revises: 9a0ef3bf74d8
Create Date: 2026-11-26 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "c5d8e1f2a3b4"
down_revision: Union[str, Sequence[str], None] = "9a0ef3bf74d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "worker_heartbeats",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("worker_name", sa.String(length=100), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tasks_done", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint("tasks_done >= 0", name="ck_worker_heartbeats_tasks_done"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("worker_name"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("worker_heartbeats")
