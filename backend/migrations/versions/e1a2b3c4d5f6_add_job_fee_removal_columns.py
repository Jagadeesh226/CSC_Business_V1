"""keep removed job fees: deleted_at and deleted_by

Revision ID: e1a2b3c4d5f6
Revises: d8e3f4a6b7c9
Create Date: 2026-11-20 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1a2b3c4d5f6"
down_revision: str | Sequence[str] | None = "d8e3f4a6b7c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "job_fees",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("job_fees", sa.Column("deleted_by", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_job_fees_deleted_by_users", "job_fees", "users", ["deleted_by"], ["id"]
    )


def downgrade() -> None:
    """Downgrade schema.

    Refuses while removed fees exist: dropping the columns would make them
    count as active fees again and silently change job totals.
    """
    removed = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM job_fees WHERE deleted_at IS NOT NULL"))
        .scalar()
    )
    if removed:
        raise RuntimeError(
            f"{removed} removed job fee(s) exist; downgrading would count them "
            "again. Resolve them before downgrading."
        )
    op.drop_constraint("fk_job_fees_deleted_by_users", "job_fees", type_="foreignkey")
    op.drop_column("job_fees", "deleted_by")
    op.drop_column("job_fees", "deleted_at")
