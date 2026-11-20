"""record who removed a job assignment

Revision ID: c5b19e7d2a41
Revises: a3d81f5c6e20
Create Date: 2026-11-20 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5b19e7d2a41"
down_revision: str | Sequence[str] | None = "a3d81f5c6e20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "job_assignments", sa.Column("unassigned_by", sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        "fk_job_assignments_unassigned_by_users",
        "job_assignments",
        "users",
        ["unassigned_by"],
        ["id"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        "fk_job_assignments_unassigned_by_users", "job_assignments", type_="foreignkey"
    )
    op.drop_column("job_assignments", "unassigned_by")
