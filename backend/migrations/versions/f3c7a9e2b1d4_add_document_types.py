"""document types shared by services; requirements reference them

- New table document_types (code, name, sensitivity, OCR fields).
- service_document_requirements now points to a document type instead of
  repeating a code and name. Existing rows are converted: one document type
  per distinct upper-cased code (trimmed to the new length limits, keeping
  the first row's name), then linked. If one service listed the same code
  twice in different letter cases, only its first row is kept. The old
  columns and unique constraint are replaced by (service_id, document_type_id).
- Category names become unique regardless of letter case.

Downgrade restores code and name columns from the linked document type, so
codes come back upper-cased and share one name per code.

Revision ID: f3c7a9e2b1d4
Revises: e1a2b3c4d5f6
Create Date: 2026-11-20 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f3c7a9e2b1d4"
down_revision: str | Sequence[str] | None = "e1a2b3c4d5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "document_types",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "sensitivity", sa.String(length=20), server_default="normal", nullable=False
        ),
        sa.Column(
            "ocr_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "sensitivity IN ('normal', 'sensitive')",
            name="ck_document_types_sensitivity",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_document_types_code"), "document_types", ["code"], unique=True
    )

    op.add_column(
        "service_document_requirements",
        sa.Column("document_type_id", sa.Integer(), nullable=True),
    )
    # Convert existing free-text requirements into document types.
    op.execute(
        """
        INSERT INTO document_types (code, name, is_active, sort_order)
        SELECT DISTINCT ON (left(upper(document_code), 50))
               left(upper(document_code), 50), left(document_name, 150), true, 0
        FROM service_document_requirements
        ORDER BY left(upper(document_code), 50), id
        """
    )
    op.execute(
        """
        UPDATE service_document_requirements r
        SET document_type_id = t.id
        FROM document_types t
        WHERE t.code = left(upper(r.document_code), 50)
        """
    )
    # One service listing the same code twice (e.g. "aadhaar" and "AADHAAR"):
    # keep the first row so the new unique constraint holds.
    op.execute(
        """
        DELETE FROM service_document_requirements r
        USING service_document_requirements keep
        WHERE keep.service_id = r.service_id
          AND keep.document_type_id = r.document_type_id
          AND keep.id < r.id
        """
    )
    op.alter_column("service_document_requirements", "document_type_id", nullable=False)
    op.create_foreign_key(
        "fk_service_document_requirements_document_type_id",
        "service_document_requirements",
        "document_types",
        ["document_type_id"],
        ["id"],
    )
    op.create_index(
        op.f("ix_service_document_requirements_document_type_id"),
        "service_document_requirements",
        ["document_type_id"],
        unique=False,
    )
    op.drop_constraint(
        "uq_service_document_requirement_code",
        "service_document_requirements",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_service_document_requirement_type",
        "service_document_requirements",
        ["service_id", "document_type_id"],
    )
    op.drop_column("service_document_requirements", "document_code")
    op.drop_column("service_document_requirements", "document_name")
    op.create_index(
        "uq_service_categories_name_lower",
        "service_categories",
        [sa.text("lower(name)")],
        unique=True,
    )


def downgrade() -> None:
    """Downgrade schema (restores code and name from the linked type)."""
    op.drop_index("uq_service_categories_name_lower", table_name="service_categories")
    op.add_column(
        "service_document_requirements",
        sa.Column("document_code", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "service_document_requirements",
        sa.Column("document_name", sa.String(length=200), nullable=True),
    )
    op.execute(
        """
        UPDATE service_document_requirements r
        SET document_code = t.code, document_name = t.name
        FROM document_types t
        WHERE t.id = r.document_type_id
        """
    )
    op.alter_column("service_document_requirements", "document_code", nullable=False)
    op.alter_column("service_document_requirements", "document_name", nullable=False)
    op.drop_constraint(
        "uq_service_document_requirement_type",
        "service_document_requirements",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_service_document_requirement_code",
        "service_document_requirements",
        ["service_id", "document_code"],
    )
    op.drop_index(
        op.f("ix_service_document_requirements_document_type_id"),
        table_name="service_document_requirements",
    )
    op.drop_constraint(
        "fk_service_document_requirements_document_type_id",
        "service_document_requirements",
        type_="foreignkey",
    )
    op.drop_column("service_document_requirements", "document_type_id")
    op.drop_index(op.f("ix_document_types_code"), table_name="document_types")
    op.drop_table("document_types")
