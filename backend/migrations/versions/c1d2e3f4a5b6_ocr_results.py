"""ocr results (Phase 2.3)

One row per OCR run of a document version, with the reviewer's confirmed
values. Extracted values and raw text are stored encrypted.

Revision ID: c1d2e3f4a5b6
Revises: b9c0d1e2f3a4
Create Date: 2026-11-21 17:31:00.727525

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, Sequence[str], None] = "b9c0d1e2f3a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "ocr_results",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("document_version", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("provider_version", sa.String(length=100), nullable=False),
        sa.Column(
            "confidences", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("extraction_sealed", sa.LargeBinary(), nullable=True),
        sa.Column("failure_reason", sa.String(length=500), nullable=True),
        sa.Column("review_sealed", sa.LargeBinary(), nullable=True),
        sa.Column("reviewed_by", sa.Integer(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('completed', 'failed')", name="ck_ocr_results_status"
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by"], ["users.id"], name="fk_ocr_results_reviewed_by_users"
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["background_tasks.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_id", "document_version", name="uq_ocr_results_document_version"
        ),
    )
    op.create_index(
        op.f("ix_ocr_results_document_id"), "ocr_results", ["document_id"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_ocr_results_document_id"), table_name="ocr_results")
    op.drop_table("ocr_results")
