from typing import Annotated

from fastapi import APIRouter, Path, Query, status

from app.api.v1.routes.jobs import RULE_422
from app.core.dependencies import AUTH_RESPONSES, AdminUser, CurrentUser, DbSession
from app.schemas.catalogue import (
    CategoryCreate,
    CategoryRead,
    CategoryUpdate,
    DocumentTypeCreate,
    DocumentTypeRead,
    DocumentTypeUpdate,
    RequirementCreate,
    RequirementRead,
    RequirementUpdate,
    ServiceCreate,
    ServiceRead,
    ServiceUpdate,
)
from app.schemas.error import ErrorResponse
from app.schemas.job import MAX_DB_INT
from app.services import catalogue_service as svc

# Everyone signed in reads the catalogue; only admins change it.
router = APIRouter(tags=["catalogue"], responses=AUTH_RESPONSES)

Id = Annotated[int, Path(gt=0, le=MAX_DB_INT)]
Active = Annotated[
    bool | None, Query(description="true: only active; false: only inactive")
]
NOT_FOUND = {404: {"model": ErrorResponse, "description": "Not found"}}
CONFLICT = {409: {"model": ErrorResponse, "description": "Code or name already used"}}
WRITE = {**NOT_FOUND, **CONFLICT, 422: RULE_422}


@router.get("/service-categories", response_model=list[CategoryRead])
def list_categories(user: CurrentUser, db: DbSession, active: Active = None):
    return svc.list_categories(db, active)


@router.post(
    "/service-categories",
    response_model=CategoryRead,
    status_code=status.HTTP_201_CREATED,
    responses=CONFLICT,
)
def create_category(payload: CategoryCreate, admin: AdminUser, db: DbSession):
    return svc.create_category(db, payload, admin)


@router.patch(
    "/service-categories/{category_id}", response_model=CategoryRead, responses=WRITE
)
def update_category(
    category_id: Id, payload: CategoryUpdate, admin: AdminUser, db: DbSession
):
    return svc.update_category(db, category_id, payload, admin)


@router.get("/services", response_model=list[ServiceRead])
def list_services(
    user: CurrentUser,
    db: DbSession,
    active: Active = None,
    category_id: Annotated[int | None, Query(gt=0, le=MAX_DB_INT)] = None,
):
    return svc.list_services(db, active, category_id)


@router.post(
    "/services",
    response_model=ServiceRead,
    status_code=status.HTTP_201_CREATED,
    responses=WRITE,
)
def create_service(payload: ServiceCreate, admin: AdminUser, db: DbSession):
    return svc.create_service(db, payload, admin)


@router.get("/services/{service_id}", response_model=ServiceRead, responses=NOT_FOUND)
def get_service(service_id: Id, user: CurrentUser, db: DbSession):
    return svc.get_service(db, service_id)


@router.patch("/services/{service_id}", response_model=ServiceRead, responses=WRITE)
def update_service(
    service_id: Id, payload: ServiceUpdate, admin: AdminUser, db: DbSession
):
    return svc.update_service(db, service_id, payload, admin)


@router.get(
    "/services/{service_id}/document-requirements",
    response_model=list[RequirementRead],
    responses=NOT_FOUND,
)
def list_requirements(
    service_id: Id, user: CurrentUser, db: DbSession, active: Active = None
):
    return svc.list_requirements(db, service_id, active)


@router.post(
    "/services/{service_id}/document-requirements",
    response_model=RequirementRead,
    status_code=status.HTTP_201_CREATED,
    responses=WRITE,
)
def add_requirement(
    service_id: Id, payload: RequirementCreate, admin: AdminUser, db: DbSession
):
    return svc.add_requirement(db, service_id, payload, admin)


@router.patch(
    "/services/{service_id}/document-requirements/{requirement_id}",
    response_model=RequirementRead,
    responses=NOT_FOUND,
)
def update_requirement(
    service_id: Id,
    requirement_id: Id,
    payload: RequirementUpdate,
    admin: AdminUser,
    db: DbSession,
):
    return svc.update_requirement(db, service_id, requirement_id, payload, admin)


@router.get("/document-types", response_model=list[DocumentTypeRead])
def list_document_types(user: CurrentUser, db: DbSession, active: Active = None):
    return svc.list_document_types(db, active)


@router.post(
    "/document-types",
    response_model=DocumentTypeRead,
    status_code=status.HTTP_201_CREATED,
    responses=CONFLICT,
)
def create_document_type(payload: DocumentTypeCreate, admin: AdminUser, db: DbSession):
    return svc.create_document_type(db, payload, admin)


@router.patch(
    "/document-types/{type_id}", response_model=DocumentTypeRead, responses=NOT_FOUND
)
def update_document_type(
    type_id: Id, payload: DocumentTypeUpdate, admin: AdminUser, db: DbSession
):
    return svc.update_document_type(db, type_id, payload, admin)
