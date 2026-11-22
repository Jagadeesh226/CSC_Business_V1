from datetime import date
from typing import Annotated

from fastapi import APIRouter, Path, Query, Response

from app.core.dependencies import AUTH_RESPONSES, CurrentUser, DbSession
from app.schemas.error import ErrorResponse
from app.schemas.job import MAX_DB_INT
from app.schemas.payment import PaymentMethod
from app.schemas.receipt import (
    ReceiptDetail,
    ReceiptFilters,
    ReceiptListResponse,
    ReceiptStatus,
)
from app.services import receipt_service

# Access: admins see every receipt; employees see receipts of jobs they are
# assigned to; others are 404. Printing is audited.
router = APIRouter(prefix="/receipts", tags=["receipts"], responses=AUTH_RESPONSES)

Id = Annotated[int, Path(gt=0, le=MAX_DB_INT)]
OptionalId = Annotated[int | None, Query(gt=0, le=MAX_DB_INT)]
NOT_FOUND = {404: {"model": ErrorResponse, "description": "Receipt not found"}}


@router.get("", response_model=ReceiptListResponse)
def search_receipts(
    user: CurrentUser,
    db: DbSession,
    page: Annotated[int, Query(ge=1, le=1_000_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    search: Annotated[
        str | None,
        Query(
            max_length=100, description="Receipt number, job number or customer name"
        ),
    ] = None,
    customer_id: OptionalId = None,
    job_id: OptionalId = None,
    method: PaymentMethod | None = None,
    status: ReceiptStatus | None = None,
    date_from: Annotated[
        date | None, Query(description="Issued on or after (centre's calendar)")
    ] = None,
    date_to: Annotated[
        date | None, Query(description="Issued on or before (centre's calendar)")
    ] = None,
) -> ReceiptListResponse:
    filters = ReceiptFilters(
        search=search,
        customer_id=customer_id,
        job_id=job_id,
        method=method,
        status=status,
        date_from=date_from,
        date_to=date_to,
    )
    return receipt_service.search(db, filters, user, page=page, page_size=page_size)


@router.get("/{receipt_id}", response_model=ReceiptDetail, responses=NOT_FOUND)
def get_receipt(receipt_id: Id, user: CurrentUser, db: DbSession) -> ReceiptDetail:
    return receipt_service.get_receipt(db, receipt_id, user)


@router.post(
    "/{receipt_id}/print",
    response_class=Response,
    responses={
        200: {
            "content": {
                "application/pdf": {"schema": {"type": "string", "format": "binary"}}
            },
            "description": "The receipt PDF",
        },
        **NOT_FOUND,
    },
)
def print_receipt(receipt_id: Id, user: CurrentUser, db: DbSession) -> Response:
    """Print one copy: the first is the original, every later one says
    DUPLICATE COPY. A POST (CSRF-protected), so reloading a page, a
    prefetch or a link from another site can never use up the original."""
    filename, pdf = receipt_service.print_receipt(db, receipt_id, user)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
