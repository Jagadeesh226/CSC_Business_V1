from typing import Annotated

from fastapi import APIRouter, Path, Query, status

from app.core.dependencies import AUTH_RESPONSES, CurrentUser, DbSession
from app.schemas.error import ErrorResponse
from app.schemas.job import MAX_DB_INT
from app.schemas.vehicle import (
    ExpiryHistoryResponse,
    ExpirySet,
    ExpiryType,
    VehicleCategory,
    VehicleCreate,
    VehicleListResponse,
    VehicleRead,
    VehicleUpdate,
)
from app.services import vehicle_service

# Access: admins see every vehicle; employees see vehicles they are
# responsible for and vehicles of customers they have an open job for
# (others are 404). Rules live in vehicle_service.
router = APIRouter(prefix="/vehicles", tags=["vehicles"], responses=AUTH_RESPONSES)

Id = Annotated[int, Path(gt=0, le=MAX_DB_INT)]
OptionalId = Annotated[int | None, Query(gt=0, le=MAX_DB_INT)]
NOT_FOUND = {404: {"model": ErrorResponse, "description": "Vehicle not found"}}
WRITE = {
    **NOT_FOUND,
    403: {"model": ErrorResponse, "description": "Not allowed for this user"},
    409: {"model": ErrorResponse, "description": "Registration number in use"},
}


@router.get("", response_model=VehicleListResponse)
def list_vehicles(
    user: CurrentUser,
    db: DbSession,
    page: Annotated[int, Query(ge=1, le=1_000_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    search: Annotated[
        str | None,
        Query(
            max_length=100,
            description="Registration number (any spacing), owner or customer name",
        ),
    ] = None,
    customer_id: OptionalId = None,
    assigned_to: OptionalId = None,
    category: VehicleCategory | None = None,
    is_active: Annotated[
        bool | None, Query(description="Default: active only; omit with null")
    ] = True,
) -> VehicleListResponse:
    return vehicle_service.list_vehicles(
        db,
        user,
        page=page,
        page_size=page_size,
        search=search,
        customer_id=customer_id,
        assigned_to=assigned_to,
        category=category.value if category else None,
        is_active=is_active,
    )


@router.post(
    "",
    response_model=VehicleRead,
    status_code=status.HTTP_201_CREATED,
    responses={
        **WRITE,
        404: {"model": ErrorResponse, "description": "Customer not found"},
    },
)
def create_vehicle(
    payload: VehicleCreate, user: CurrentUser, db: DbSession
) -> VehicleRead:
    return vehicle_service.create_vehicle(db, payload, user)


@router.get(
    "/{vehicle_id}",
    response_model=VehicleRead,
    responses={
        **NOT_FOUND,
        403: {"model": ErrorResponse, "description": "Not allowed to reveal"},
    },
)
def get_vehicle(
    vehicle_id: Id,
    user: CurrentUser,
    db: DbSession,
    reveal: Annotated[
        bool,
        Query(
            description=(
                "Show full chassis and engine numbers (admin or the responsible "
                "employee; audited)"
            )
        ),
    ] = False,
) -> VehicleRead:
    return vehicle_service.get_vehicle(db, vehicle_id, user, reveal)


@router.patch("/{vehicle_id}", response_model=VehicleRead, responses=WRITE)
def update_vehicle(
    vehicle_id: Id, payload: VehicleUpdate, user: CurrentUser, db: DbSession
) -> VehicleRead:
    return vehicle_service.update_vehicle(db, vehicle_id, payload, user)


@router.put(
    "/{vehicle_id}/expiries/{expiry_type}",
    response_model=VehicleRead,
    responses=NOT_FOUND,
)
def set_expiry(
    vehicle_id: Id,
    expiry_type: ExpiryType,
    payload: ExpirySet,
    user: CurrentUser,
    db: DbSession,
) -> VehicleRead:
    """Set (or clear with null) the current date; the old one stays in the
    history."""
    return vehicle_service.set_expiry(db, vehicle_id, expiry_type, payload, user)


@router.get(
    "/{vehicle_id}/expiries/{expiry_type}/history",
    response_model=ExpiryHistoryResponse,
    responses=NOT_FOUND,
)
def expiry_history(
    vehicle_id: Id, expiry_type: ExpiryType, user: CurrentUser, db: DbSession
) -> ExpiryHistoryResponse:
    return vehicle_service.expiry_history(db, vehicle_id, expiry_type, user)
