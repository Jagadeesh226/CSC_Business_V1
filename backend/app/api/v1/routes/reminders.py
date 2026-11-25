from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.core.dependencies import AUTH_RESPONSES, CurrentUser, DbSession
from app.schemas.error import ErrorResponse
from app.schemas.job import MAX_DB_INT
from app.schemas.reminder import (
    DismissRequest,
    DoneRequest,
    ExpiryBucket,
    ExpiryListResponse,
    NotificationListResponse,
    ReminderListResponse,
    ReminderRead,
    ReminderStatus,
)
from app.schemas.vehicle import ExpiryType
from app.services import reminder_service

# Reminders and expiries follow vehicle access (admins: all; employees:
# vehicles they are responsible for or whose customer they have an open
# job for). Notifications are each user's own.
router = APIRouter(tags=["reminders"], responses=AUTH_RESPONSES)

Id = Annotated[int, Path(gt=0, le=MAX_DB_INT)]
Page = Annotated[int, Query(ge=1, le=1_000_000)]
PageSize = Annotated[int, Query(ge=1, le=100)]
NOT_FOUND = {404: {"model": ErrorResponse, "description": "Reminder not found"}}


@router.get("/reminders", response_model=ReminderListResponse)
def list_reminders(
    user: CurrentUser,
    db: DbSession,
    page: Page = 1,
    page_size: PageSize = 20,
    status: ReminderStatus | None = None,
    vehicle_id: Annotated[int | None, Query(gt=0, le=MAX_DB_INT)] = None,
    expiry_type: ExpiryType | None = None,
) -> ReminderListResponse:
    """Soonest first. ``status=open`` gives pending and ready_to_send."""
    return reminder_service.list_reminders(
        db,
        user,
        page=page,
        page_size=page_size,
        status=status,
        vehicle_id=vehicle_id,
        expiry_type=expiry_type.value if expiry_type else None,
    )


@router.post(
    "/reminders/{reminder_id}/done", response_model=ReminderRead, responses=NOT_FOUND
)
def mark_done(
    reminder_id: Id, payload: DoneRequest, user: CurrentUser, db: DbSession
) -> ReminderRead:
    """Record that the customer was contacted (e.g. the WhatsApp message was
    sent). Opening the WhatsApp link does not do this."""
    return reminder_service.mark_done(db, reminder_id, user, payload.note)


@router.post(
    "/reminders/{reminder_id}/dismiss", response_model=ReminderRead, responses=NOT_FOUND
)
def dismiss(
    reminder_id: Id, payload: DismissRequest, user: CurrentUser, db: DbSession
) -> ReminderRead:
    return reminder_service.dismiss(db, reminder_id, user, payload.reason)


@router.get("/vehicle-expiries", response_model=ExpiryListResponse)
def list_expiries(
    user: CurrentUser,
    db: DbSession,
    page: Page = 1,
    page_size: PageSize = 20,
    bucket: ExpiryBucket | None = None,
    expiry_type: ExpiryType | None = None,
) -> ExpiryListResponse:
    """Current validity dates of active vehicles in buckets: expired, 0-7,
    8-15, 16-30 and over 30 days (centre's calendar)."""
    return reminder_service.list_expiries(
        db,
        user,
        page=page,
        page_size=page_size,
        bucket=bucket,
        expiry_type=expiry_type.value if expiry_type else None,
    )


@router.get("/notifications", response_model=NotificationListResponse)
def list_notifications(
    user: CurrentUser,
    db: DbSession,
    page: Page = 1,
    page_size: PageSize = 20,
    unread: bool = False,
) -> NotificationListResponse:
    return reminder_service.list_notifications(
        db, user, unread_only=unread, page=page, page_size=page_size
    )


@router.post(
    "/notifications/{notification_id}/read",
    responses={404: {"model": ErrorResponse, "description": "Notification not found"}},
)
def mark_read(notification_id: Id, user: CurrentUser, db: DbSession) -> dict[str, int]:
    return {"marked": reminder_service.mark_read(db, user, notification_id)}


@router.post("/notifications/read-all")
def mark_all_read(user: CurrentUser, db: DbSession) -> dict[str, int]:
    return {"marked": reminder_service.mark_read(db, user, None)}
