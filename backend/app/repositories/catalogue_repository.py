from sqlalchemy import Row, select
from sqlalchemy.orm import Session

from app.models import (
    DocumentType,
    Service,
    ServiceCategory,
    ServiceDocumentRequirement,
)


def get(db: Session, model, obj_id: int):
    return db.get(model, obj_id)


def add(db: Session, obj):
    db.add(obj)
    db.flush()
    return obj


def code_taken(db: Session, model, code: str, exclude_id: int | None = None) -> bool:
    stmt = select(model.id).where(model.code == code)
    if exclude_id is not None:
        stmt = stmt.where(model.id != exclude_id)
    return db.scalar(stmt) is not None


def name_taken(db: Session, model, name: str, exclude_id: int | None = None) -> bool:
    stmt = select(model.id).where(model.name == name)
    if exclude_id is not None:
        stmt = stmt.where(model.id != exclude_id)
    return db.scalar(stmt) is not None


def list_categories(db: Session, active: bool | None) -> list[ServiceCategory]:
    stmt = select(ServiceCategory).order_by(
        ServiceCategory.sort_order, ServiceCategory.name
    )
    if active is not None:
        stmt = stmt.where(ServiceCategory.is_active.is_(active))
    return list(db.scalars(stmt))


ServiceRow = Row[tuple[Service, ServiceCategory]]


def _services():
    return select(Service, ServiceCategory).join(
        ServiceCategory, Service.category_id == ServiceCategory.id
    )


def list_services(
    db: Session, active: bool | None, category_id: int | None
) -> list[ServiceRow]:
    stmt = _services().order_by(
        ServiceCategory.sort_order,
        ServiceCategory.name,
        Service.sort_order,
        Service.name,
    )
    if active is not None:
        # "Active" for the dropdown means the service and its category.
        stmt = stmt.where(Service.is_active.is_(active))
        if active:
            stmt = stmt.where(ServiceCategory.is_active.is_(True))
    if category_id is not None:
        stmt = stmt.where(Service.category_id == category_id)
    return list(db.execute(stmt).all())


def get_service_row(db: Session, service_id: int) -> ServiceRow | None:
    return db.execute(_services().where(Service.id == service_id)).first()


def list_document_types(db: Session, active: bool | None) -> list[DocumentType]:
    stmt = select(DocumentType).order_by(DocumentType.sort_order, DocumentType.name)
    if active is not None:
        stmt = stmt.where(DocumentType.is_active.is_(active))
    return list(db.scalars(stmt))


RequirementRow = Row[tuple[ServiceDocumentRequirement, DocumentType]]


def list_requirements(
    db: Session, service_id: int, active: bool | None
) -> list[RequirementRow]:
    stmt = (
        select(ServiceDocumentRequirement, DocumentType)
        .join(
            DocumentType, ServiceDocumentRequirement.document_type_id == DocumentType.id
        )
        .where(ServiceDocumentRequirement.service_id == service_id)
        .order_by(ServiceDocumentRequirement.sort_order, ServiceDocumentRequirement.id)
    )
    if active is not None:
        stmt = stmt.where(ServiceDocumentRequirement.is_active.is_(active))
    return list(db.execute(stmt).all())


def requirement_exists(db: Session, service_id: int, document_type_id: int) -> bool:
    return (
        db.scalar(
            select(ServiceDocumentRequirement.id).where(
                ServiceDocumentRequirement.service_id == service_id,
                ServiceDocumentRequirement.document_type_id == document_type_id,
            )
        )
        is not None
    )
