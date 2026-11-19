from typing import Annotated

from fastapi import APIRouter, Path, Query, status

from app.core.dependencies import AUTH_RESPONSES, AdminUser, DbSession
from app.schemas.error import ErrorResponse
from app.schemas.job import MAX_DB_INT
from app.schemas.user import (
    PasswordReset,
    UserCreate,
    UserListResponse,
    UserRead,
    UserRole,
    UserUpdate,
)
from app.services import user_service

# Admin only: managing employee accounts (docs/contracts/permissions.md).
router = APIRouter(prefix="/users", tags=["users"], responses=AUTH_RESPONSES)

UserId = Annotated[int, Path(gt=0, le=MAX_DB_INT)]
NOT_FOUND = {404: {"model": ErrorResponse, "description": "User not found"}}
RULE = {422: {"model": ErrorResponse, "description": "Validation or business rule"}}


@router.post(
    "",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": ErrorResponse, "description": "Email already used"}},
)
def create_user(payload: UserCreate, admin: AdminUser, db: DbSession) -> UserRead:
    return user_service.create_user(db, admin, payload)


@router.get("", response_model=UserListResponse)
def list_users(
    admin: AdminUser,
    db: DbSession,
    page: Annotated[int, Query(ge=1, le=1_000_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    search: Annotated[str | None, Query(max_length=200)] = None,
    role: UserRole | None = None,
    is_active: bool | None = None,
) -> UserListResponse:
    return user_service.list_users(
        db,
        page=page,
        page_size=page_size,
        search=search,
        role=role,
        is_active=is_active,
    )


@router.get("/{user_id}", response_model=UserRead, responses=NOT_FOUND)
def get_user(user_id: UserId, admin: AdminUser, db: DbSession) -> UserRead:
    return user_service.get_user(db, user_id)


@router.patch(
    "/{user_id}",
    response_model=UserRead,
    responses={
        **NOT_FOUND,
        **RULE,
        409: {"model": ErrorResponse, "description": "Email already used"},
    },
)
def update_user(
    user_id: UserId, payload: UserUpdate, admin: AdminUser, db: DbSession
) -> UserRead:
    return user_service.update_user(db, admin, user_id, payload)


@router.post(
    "/{user_id}/deactivate", response_model=UserRead, responses={**NOT_FOUND, **RULE}
)
def deactivate_user(user_id: UserId, admin: AdminUser, db: DbSession) -> UserRead:
    return user_service.deactivate_user(db, admin, user_id)


@router.post("/{user_id}/reactivate", response_model=UserRead, responses=NOT_FOUND)
def reactivate_user(user_id: UserId, admin: AdminUser, db: DbSession) -> UserRead:
    return user_service.reactivate_user(db, admin, user_id)


@router.post("/{user_id}/reset-password", response_model=UserRead, responses=NOT_FOUND)
def reset_password(
    user_id: UserId, payload: PasswordReset, admin: AdminUser, db: DbSession
) -> UserRead:
    return user_service.reset_password(db, admin, user_id, payload.temporary_password)
