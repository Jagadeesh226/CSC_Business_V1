from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    DocumentType,
    Service,
    ServiceCategory,
    ServiceDocumentRequirement,
    User,
)
from app.repositories import catalogue_repository as repo
from app.schemas.catalogue import (
    CategoryCreate,
    CategoryRead,
    CategoryRef,
    CategoryUpdate,
    DocumentTypeCreate,
    DocumentTypeRead,
    DocumentTypeRef,
    DocumentTypeUpdate,
    RequirementCreate,
    RequirementRead,
    RequirementUpdate,
    ServiceCreate,
    ServiceFeeType,
    ServiceRead,
    ServiceUpdate,
    check_vehicle_role,
)
from app.services import audit_service
from app.services.errors import BusinessRuleError, ConflictError, NotFoundError

# Everyone signed in can read the catalogue (it feeds the New Job dropdowns);
# only admins change it (enforced by the routes). Changing the catalogue never
# alters existing jobs: job checklists are copied from it when a job is made.


def _audit(
    db: Session,
    action: str,
    entity_type: str,
    entity_id: int,
    actor: User,
    fields: list[str] | None = None,
    extra: dict | None = None,
) -> None:
    details = dict(extra or {})
    if fields:
        details["fields"] = fields
    audit_service.record(
        db,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        user_id=actor.id,
        details=details or None,
    )


def _commit_unique(db: Session, message: str) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ConflictError(message) from None


def _add_unique(db: Session, obj, message: str):
    """Insert ``obj``; a concurrent duplicate becomes 409, not 500."""
    try:
        return repo.add(db, obj)
    except IntegrityError:
        db.rollback()
        raise ConflictError(message) from None


def _apply(obj, changes: dict) -> list[str]:
    changed = sorted(k for k, v in changes.items() if getattr(obj, k) != v)
    for field in changed:
        setattr(obj, field, changes[field])
    return changed


# ---------- categories ----------


def list_categories(db: Session, active: bool | None) -> list[CategoryRead]:
    return [CategoryRead.model_validate(c) for c in repo.list_categories(db, active)]


def create_category(db: Session, data: CategoryCreate, actor: User) -> CategoryRead:
    if repo.code_taken(db, ServiceCategory, data.code):
        raise ConflictError("A category with this code already exists.")
    if repo.name_taken(db, ServiceCategory, data.name):
        raise ConflictError("A category with this name already exists.")
    category = _add_unique(
        db,
        ServiceCategory(**data.model_dump()),
        "A category with this code or name already exists.",
    )
    _audit(db, "service_category_created", "service_category", category.id, actor)
    _commit_unique(db, "A category with this code or name already exists.")
    return CategoryRead.model_validate(category)


def update_category(
    db: Session, category_id: int, data: CategoryUpdate, actor: User
) -> CategoryRead:
    category = repo.get(db, ServiceCategory, category_id)
    if category is None:
        raise NotFoundError("Category not found.")
    changes = data.model_dump(exclude_unset=True)
    if "name" in changes and repo.name_taken(
        db, ServiceCategory, changes["name"], exclude_id=category.id
    ):
        raise ConflictError("A category with this name already exists.")
    changed = _apply(category, changes)
    if changed:
        _audit(
            db,
            "service_category_updated",
            "service_category",
            category.id,
            actor,
            changed,
        )
    _commit_unique(db, "A category with this name already exists.")
    return CategoryRead.model_validate(category)


# ---------- services ----------


def _service_read(service: Service, category: ServiceCategory) -> ServiceRead:
    return ServiceRead(
        id=service.id,
        category=CategoryRef.model_validate(category),
        name=service.name,
        code=service.code,
        description=service.description,
        fee_type=service.fee_type,
        default_service_fee=service.default_service_fee,
        requires_vehicle=service.requires_vehicle,
        is_active=service.is_active,
        sort_order=service.sort_order,
    )


def list_services(
    db: Session, active: bool | None, category_id: int | None
) -> list[ServiceRead]:
    return [_service_read(s, c) for s, c in repo.list_services(db, active, category_id)]


def get_service(db: Session, service_id: int) -> ServiceRead:
    row = repo.get_service_row(db, service_id)
    if row is None:
        raise NotFoundError("Service not found.")
    return _service_read(*row)


def _check_category(db: Session, category_id: int) -> None:
    category = repo.get(db, ServiceCategory, category_id)
    if category is None:
        raise NotFoundError("Category not found.")
    if not category.is_active:
        raise BusinessRuleError("Services cannot be added to an inactive category.")


def create_service(db: Session, data: ServiceCreate, actor: User) -> ServiceRead:
    _check_category(db, data.category_id)
    if repo.code_taken(db, Service, data.code):
        raise ConflictError("A service with this code already exists.")
    values = data.model_dump()
    values["fee_type"] = data.fee_type.value
    service = _add_unique(
        db, Service(**values), "A service with this code already exists."
    )
    _audit(db, "service_created", "service", service.id, actor)
    _commit_unique(db, "A service with this code already exists.")
    return get_service(db, service.id)


def update_service(
    db: Session, service_id: int, data: ServiceUpdate, actor: User
) -> ServiceRead:
    service = repo.get(db, Service, service_id)
    if service is None:
        raise NotFoundError("Service not found.")
    changes = data.model_dump(exclude_unset=True)
    if "category_id" in changes and changes["category_id"] != service.category_id:
        _check_category(db, changes["category_id"])
    if "fee_type" in changes:
        changes["fee_type"] = changes["fee_type"].value
    becomes_active = changes.get("is_active", service.is_active)
    category = repo.get(
        db, ServiceCategory, changes.get("category_id", service.category_id)
    )
    if becomes_active and (category is None or not category.is_active):
        raise BusinessRuleError(
            "A service cannot be active in an inactive category. "
            "Reactivate the category first."
        )
    fee_type = changes.get("fee_type", service.fee_type)
    fee = changes.get("default_service_fee", service.default_service_fee)
    if fee_type == ServiceFeeType.FIXED.value and fee is None:
        raise BusinessRuleError("A fixed-fee service needs a default_service_fee.")
    changed = _apply(service, changes)
    if changed:
        _audit(db, "service_updated", "service", service.id, actor, changed)
    db.commit()
    return get_service(db, service_id)


# ---------- document types ----------


def list_document_types(db: Session, active: bool | None) -> list[DocumentTypeRead]:
    return [
        DocumentTypeRead.model_validate(t) for t in repo.list_document_types(db, active)
    ]


def create_document_type(
    db: Session, data: DocumentTypeCreate, actor: User
) -> DocumentTypeRead:
    if repo.code_taken(db, DocumentType, data.code):
        raise ConflictError("A document type with this code already exists.")
    values = data.model_dump(mode="json")
    doc_type = _add_unique(
        db, DocumentType(**values), "A document type with this code already exists."
    )
    _audit(db, "document_type_created", "document_type", doc_type.id, actor)
    _commit_unique(db, "A document type with this code already exists.")
    return DocumentTypeRead.model_validate(doc_type)


def update_document_type(
    db: Session, type_id: int, data: DocumentTypeUpdate, actor: User
) -> DocumentTypeRead:
    doc_type = repo.get(db, DocumentType, type_id)
    if doc_type is None:
        raise NotFoundError("Document type not found.")
    changes = data.model_dump(mode="json", exclude_unset=True)
    try:
        check_vehicle_role(
            changes.get("vehicle_role", doc_type.vehicle_role),
            changes.get("ocr_fields", doc_type.ocr_fields),
        )
    except ValueError as exc:
        raise BusinessRuleError(str(exc)) from None
    changed = _apply(doc_type, changes)
    if changed:
        _audit(
            db, "document_type_updated", "document_type", doc_type.id, actor, changed
        )
    db.commit()
    return DocumentTypeRead.model_validate(doc_type)


# ---------- service document requirements ----------


def _requirement_read(
    req: ServiceDocumentRequirement, doc_type: DocumentType
) -> RequirementRead:
    return RequirementRead(
        id=req.id,
        document_type=DocumentTypeRef.model_validate(doc_type),
        is_required=req.is_required,
        allow_multiple=req.allow_multiple,
        is_active=req.is_active,
        description=req.description,
        sort_order=req.sort_order,
        created_at=req.created_at,
    )


def list_requirements(
    db: Session, service_id: int, active: bool | None
) -> list[RequirementRead]:
    if repo.get(db, Service, service_id) is None:
        raise NotFoundError("Service not found.")
    return [
        _requirement_read(r, t)
        for r, t in repo.list_requirements(db, service_id, active)
    ]


def add_requirement(
    db: Session, service_id: int, data: RequirementCreate, actor: User
) -> RequirementRead:
    if repo.get(db, Service, service_id) is None:
        raise NotFoundError("Service not found.")
    doc_type = repo.get(db, DocumentType, data.document_type_id)
    if doc_type is None:
        raise NotFoundError("Document type not found.")
    message = "This service already lists this document type."
    if repo.requirement_exists(db, service_id, doc_type.id):
        raise ConflictError(message)
    if not doc_type.is_active:
        raise BusinessRuleError("An inactive document type cannot be required.")
    req = _add_unique(
        db,
        ServiceDocumentRequirement(service_id=service_id, **data.model_dump()),
        message,
    )
    _audit(
        db,
        "service_requirement_added",
        "service",
        service_id,
        actor,
        extra={"requirement_id": req.id, "document_type": doc_type.code},
    )
    db.commit()
    return _requirement_read(req, doc_type)


def update_requirement(
    db: Session,
    service_id: int,
    requirement_id: int,
    data: RequirementUpdate,
    actor: User,
) -> RequirementRead:
    req = repo.get(db, ServiceDocumentRequirement, requirement_id)
    if req is None or req.service_id != service_id:
        raise NotFoundError("Requirement not found.")
    doc_type = repo.get(db, DocumentType, req.document_type_id)
    changes = data.model_dump(exclude_unset=True)
    if changes.get("is_active") and not doc_type.is_active:
        raise BusinessRuleError(
            "The document type is inactive. Reactivate it before this requirement."
        )
    changed = _apply(req, changes)
    if changed:
        _audit(
            db,
            "service_requirement_updated",
            "service",
            service_id,
            actor,
            changed,
            extra={"requirement_id": req.id, "document_type": doc_type.code},
        )
    db.commit()
    return _requirement_read(req, doc_type)
