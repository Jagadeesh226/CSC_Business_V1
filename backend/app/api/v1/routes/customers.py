from typing import Annotated

from fastapi import APIRouter, Path, Query, status

from app.core.dependencies import AUTH_RESPONSES, CurrentUser, DbSession
from app.schemas.customer import (
    CustomerCreate,
    CustomerCreated,
    CustomerListResponse,
    CustomerRead,
    CustomerUpdate,
)
from app.schemas.error import ErrorResponse
from app.schemas.job import MAX_DB_INT
from app.services import customer_service

# permissions.md: admins and employees view, create and update customers.
router = APIRouter(prefix="/customers", tags=["customers"], responses=AUTH_RESPONSES)

CustomerId = Annotated[int, Path(gt=0, le=MAX_DB_INT)]
NOT_FOUND = {404: {"model": ErrorResponse, "description": "Customer not found"}}


@router.get("", response_model=CustomerListResponse)
def list_customers(
    user: CurrentUser,
    db: DbSession,
    page: Annotated[int, Query(ge=1, le=1_000_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    search: Annotated[
        str | None,
        Query(
            max_length=200,
            description="Name, 4+ digits of the phone number, or a vehicle registration number",
        ),
    ] = None,
) -> CustomerListResponse:
    return customer_service.list_customers(
        db, page=page, page_size=page_size, search=search
    )


@router.post("", response_model=CustomerCreated, status_code=status.HTTP_201_CREATED)
def create_customer(
    payload: CustomerCreate, user: CurrentUser, db: DbSession
) -> CustomerCreated:
    return customer_service.create_customer(db, payload, actor=user)


@router.get("/{customer_id}", response_model=CustomerRead, responses=NOT_FOUND)
def get_customer(
    customer_id: CustomerId, user: CurrentUser, db: DbSession
) -> CustomerRead:
    return customer_service.get_customer(db, customer_id)


@router.patch("/{customer_id}", response_model=CustomerRead, responses=NOT_FOUND)
def update_customer(
    customer_id: CustomerId, payload: CustomerUpdate, user: CurrentUser, db: DbSession
) -> CustomerRead:
    return customer_service.update_customer(db, customer_id, payload, actor=user)
