"""restrict job statuses and start status history for existing jobs

- CHECK constraint so jobs.status holds only the six approved statuses.
  The upgrade fails (and changes nothing) if an existing job has another
  status, so bad data is never silently rewritten.
- Every job gets a first history entry (none -> pending) at its creation
  time, matching what new jobs record. A job already past 'pending' also gets
  an entry pending -> current status, marked as recorded by this migration.

Downgrade drops the constraint and keeps the history rows: they are data, and
the earlier schema already had the table.

Revision ID: d8e3f4a6b7c9
Revises: c5b19e7d2a41
Create Date: 2026-11-20 14:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d8e3f4a6b7c9"
down_revision: str | Sequence[str] | None = "c5b19e7d2a41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = (
    "'pending', 'in_progress', 'waiting_for_documents', "
    "'submitted', 'completed', 'cancelled'"
)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_check_constraint("ck_jobs_status", "jobs", f"status IN ({STATUSES})")
    op.execute(
        """
        INSERT INTO job_status_history (job_id, old_status, new_status, changed_by,
                                        changed_at)
        SELECT j.id, NULL, 'pending', j.created_by, j.created_at
        FROM jobs j
        WHERE NOT EXISTS (
            SELECT 1 FROM job_status_history h WHERE h.job_id = j.id
        )
        """
    )
    op.execute(
        """
        INSERT INTO job_status_history (job_id, old_status, new_status, reason)
        SELECT j.id, 'pending', j.status,
               'Recorded when status history was introduced'
        FROM jobs j
        WHERE j.status <> 'pending'
          AND NOT EXISTS (
              SELECT 1 FROM job_status_history h
              WHERE h.job_id = j.id AND h.new_status = j.status
          )
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("ck_jobs_status", "jobs", type_="check")
