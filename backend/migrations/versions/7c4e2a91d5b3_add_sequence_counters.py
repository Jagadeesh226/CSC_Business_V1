"""add sequence counters for server-generated numbers

Needed for concurrency-safe job numbers (CSC-YYYY-NNNNNN). Each allocation is an
atomic upsert on (name, year); the row lock serialises concurrent allocations
within one year, so no two transactions can receive the same number.

Revision ID: 7c4e2a91d5b3
Revises: 31b33db4529a
Create Date: 2026-11-18 19:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c4e2a91d5b3"
down_revision: str | Sequence[str] | None = "31b33db4529a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "sequence_counters",
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("last_value", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "last_value > 0", name="ck_sequence_counters_last_value_positive"
        ),
        sa.PrimaryKeyConstraint("name", "year"),
    )
    # Seed job-number counters from any existing CSC-YYYY-NNNNNN numbers so the
    # first allocation in each year continues after them instead of colliding.
    op.execute(
        """
        INSERT INTO sequence_counters (name, year, last_value)
        SELECT 'job_number',
               CAST(substring(job_number FROM 5 FOR 4) AS integer),
               MAX(CAST(substring(job_number FROM 10) AS bigint))
        FROM jobs
        WHERE job_number ~ '^CSC-[0-9]{4}-[0-9]+$'
        GROUP BY 2
        HAVING MAX(CAST(substring(job_number FROM 10) AS bigint)) > 0
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("sequence_counters")
