"""documents lifecycle, versions and job checklists (Phase 2.1)

- documents gain a type, file details (size, SHA-256, pages), a current
  version, a status (uploaded/processing/needs_review/verified/rejected),
  verification and archive fields; customer_id becomes required.
- document_versions: immutable stored files, one row per version.
- job_checklist_items: each job's document checklist, frozen from its
  service's active requirements. Existing open jobs get one now.
- job_documents: documents used by a job (and which checklist item), so a
  verified document can serve many jobs.
- jobs.service_name: the service name at creation (filled for existing jobs).

The upgrade refuses to run if the documents table already has rows: no API
wrote documents before, so any rows are unexplained and need a manual look
rather than invented file details. The downgrade refuses while documents
exist, since it would drop their versions, links and checklist state.

Revision ID: a7b8c9d0e1f2
Revises: f3c7a9e2b1d4
Create Date: 2026-11-21 10:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, Sequence[str], None] = "f3c7a9e2b1d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _refuse_if_documents_exist(action: str) -> None:
    count = op.get_bind().execute(sa.text("SELECT count(*) FROM documents")).scalar()
    if count:
        raise RuntimeError(
            f"{count} document row(s) exist; refusing to {action}. "
            "Review them manually first."
        )


def upgrade() -> None:
    """Upgrade schema."""
    _refuse_if_documents_exist("upgrade")
    op.create_table(
        "job_checklist_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("document_type_id", sa.Integer(), nullable=False),
        sa.Column("document_type_code", sa.String(length=50), nullable=False),
        sa.Column("document_type_name", sa.String(length=150), nullable=False),
        sa.Column("sensitivity", sa.String(length=20), nullable=False),
        sa.Column(
            "ocr_fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("is_required", sa.Boolean(), nullable=False),
        sa.Column("allow_multiple", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_type_id"],
            ["document_types.id"],
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "document_type_id", name="uq_job_checklist_type"),
    )
    op.create_index(
        op.f("ix_job_checklist_items_job_id"),
        "job_checklist_items",
        ["job_id"],
        unique=False,
    )
    op.create_table(
        "document_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.String(length=500), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("change_reason", sa.Text(), nullable=True),
        sa.Column("uploaded_by", sa.Integer(), nullable=True),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_id", "version", name="uq_document_versions_number"
        ),
    )
    op.create_index(
        op.f("ix_document_versions_document_id"),
        "document_versions",
        ["document_id"],
        unique=False,
    )
    op.create_table(
        "job_documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("checklist_item_id", sa.Integer(), nullable=True),
        sa.Column("linked_by", sa.Integer(), nullable=True),
        sa.Column(
            "linked_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["checklist_item_id"],
            ["job_checklist_items.id"],
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["linked_by"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "document_id", name="uq_job_documents_link"),
    )
    op.create_index(
        op.f("ix_job_documents_checklist_item_id"),
        "job_documents",
        ["checklist_item_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_job_documents_document_id"),
        "job_documents",
        ["document_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_job_documents_job_id"), "job_documents", ["job_id"], unique=False
    )
    op.add_column(
        "documents", sa.Column("document_type_id", sa.Integer(), nullable=True)
    )
    op.add_column("documents", sa.Column("size_bytes", sa.BigInteger(), nullable=False))
    op.add_column(
        "documents", sa.Column("sha256", sa.String(length=64), nullable=False)
    )
    op.add_column("documents", sa.Column("page_count", sa.Integer(), nullable=True))
    op.add_column(
        "documents", sa.Column("current_version", sa.Integer(), nullable=False)
    )
    op.add_column(
        "documents", sa.Column("status", sa.String(length=20), nullable=False)
    )
    op.add_column("documents", sa.Column("rejection_reason", sa.Text(), nullable=True))
    op.add_column("documents", sa.Column("verified_by", sa.Integer(), nullable=True))
    op.add_column(
        "documents", sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "documents", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("documents", sa.Column("archived_by", sa.Integer(), nullable=True))
    op.add_column("documents", sa.Column("archive_reason", sa.Text(), nullable=True))
    op.alter_column(
        "documents", "customer_id", existing_type=sa.INTEGER(), nullable=False
    )
    op.create_index(
        op.f("ix_documents_customer_id"), "documents", ["customer_id"], unique=False
    )
    op.create_index(
        op.f("ix_documents_document_type_id"),
        "documents",
        ["document_type_id"],
        unique=False,
    )
    op.create_index(op.f("ix_documents_status"), "documents", ["status"], unique=False)
    op.create_foreign_key(
        "fk_documents_document_type_id",
        "documents",
        "document_types",
        ["document_type_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_documents_verified_by_users", "documents", "users", ["verified_by"], ["id"]
    )
    op.create_foreign_key(
        "fk_documents_archived_by_users", "documents", "users", ["archived_by"], ["id"]
    )
    op.create_check_constraint(
        "ck_documents_status",
        "documents",
        "status IN ('uploaded', 'processing', 'needs_review', 'verified', 'rejected')",
    )
    op.add_column(
        "jobs", sa.Column("service_name", sa.String(length=200), nullable=True)
    )
    op.execute(
        """
        UPDATE jobs j SET service_name = s.name
        FROM services s WHERE s.id = j.service_id
        """
    )
    # Open jobs get the checklist their service asks for today.
    op.execute(
        """
        INSERT INTO job_checklist_items (
            job_id, document_type_id, document_type_code, document_type_name,
            sensitivity, ocr_fields, is_required, allow_multiple, sort_order
        )
        SELECT j.id, t.id, t.code, t.name, t.sensitivity, t.ocr_fields,
               r.is_required, r.allow_multiple, r.sort_order
        FROM jobs j
        JOIN service_document_requirements r ON r.service_id = j.service_id
        JOIN document_types t ON t.id = r.document_type_id
        WHERE j.status NOT IN ('completed', 'cancelled')
          AND r.is_active AND t.is_active
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    _refuse_if_documents_exist("downgrade")
    op.drop_constraint("ck_documents_status", "documents", type_="check")
    op.drop_column("jobs", "service_name")
    op.drop_constraint(
        "fk_documents_archived_by_users", "documents", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_documents_verified_by_users", "documents", type_="foreignkey"
    )
    op.drop_constraint("fk_documents_document_type_id", "documents", type_="foreignkey")
    op.drop_index(op.f("ix_documents_status"), table_name="documents")
    op.drop_index(op.f("ix_documents_document_type_id"), table_name="documents")
    op.drop_index(op.f("ix_documents_customer_id"), table_name="documents")
    op.alter_column(
        "documents", "customer_id", existing_type=sa.INTEGER(), nullable=True
    )
    op.drop_column("documents", "archive_reason")
    op.drop_column("documents", "archived_by")
    op.drop_column("documents", "archived_at")
    op.drop_column("documents", "verified_at")
    op.drop_column("documents", "verified_by")
    op.drop_column("documents", "rejection_reason")
    op.drop_column("documents", "status")
    op.drop_column("documents", "current_version")
    op.drop_column("documents", "page_count")
    op.drop_column("documents", "sha256")
    op.drop_column("documents", "size_bytes")
    op.drop_column("documents", "document_type_id")
    op.drop_index(op.f("ix_job_documents_job_id"), table_name="job_documents")
    op.drop_index(op.f("ix_job_documents_document_id"), table_name="job_documents")
    op.drop_index(
        op.f("ix_job_documents_checklist_item_id"), table_name="job_documents"
    )
    op.drop_table("job_documents")
    op.drop_index(
        op.f("ix_document_versions_document_id"), table_name="document_versions"
    )
    op.drop_table("document_versions")
    op.drop_index(
        op.f("ix_job_checklist_items_job_id"), table_name="job_checklist_items"
    )
    op.drop_table("job_checklist_items")
