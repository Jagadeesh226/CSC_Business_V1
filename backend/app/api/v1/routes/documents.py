from typing import Annotated

from fastapi import APIRouter, File, Form, Path, Query, Response, UploadFile, status
from fastapi.concurrency import run_in_threadpool

from app.api.v1.routes.jobs import NOT_FOUND as JOB_NOT_FOUND
from app.api.v1.routes.jobs import RULE_422, JobId
from app.core.dependencies import AUTH_RESPONSES, AdminUser, CurrentUser, DbSession
from app.schemas.document import (
    ArchiveRequest,
    ChecklistResponse,
    DocumentDetail,
    DocumentRead,
    DocumentStatus,
    LinkDocumentRequest,
    RejectRequest,
)
from app.schemas.error import ErrorResponse
from app.schemas.job import MAX_DB_INT
from app.services import document_service as svc
from app.services.file_validation import MAX_BYTES, FileTooLargeError, check_upload

router = APIRouter(tags=["documents"], responses=AUTH_RESPONSES)

Id = Annotated[int, Path(gt=0, le=MAX_DB_INT)]
OptionalId = Annotated[int | None, Form(gt=0, le=MAX_DB_INT)]
NOT_FOUND = {404: {"model": ErrorResponse, "description": "Not found"}}
WRITE = {**NOT_FOUND, 409: {"model": ErrorResponse}, 422: RULE_422}
UPLOAD = {**WRITE, 413: {"model": ErrorResponse, "description": "File too large"}}


async def _read_limited(upload: UploadFile) -> bytes:
    """Read at most MAX_BYTES (+1 to detect larger files) without loading more."""
    chunks, total = [], 0
    while chunk := await upload.read(1024 * 1024):
        total += len(chunk)
        if total > MAX_BYTES:
            raise FileTooLargeError("Files can be at most 10 MB.")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post(
    "/customers/{customer_id}/documents",
    response_model=DocumentDetail,
    status_code=status.HTTP_201_CREATED,
    responses=UPLOAD,
)
async def upload_document(
    customer_id: Id,
    user: CurrentUser,
    db: DbSession,
    file: Annotated[UploadFile, File(description="PDF, PNG or JPEG, up to 10 MB")],
    document_type_id: OptionalId = None,
    job_id: OptionalId = None,
    checklist_item_id: OptionalId = None,
) -> DocumentDetail:
    data = await _read_limited(file)
    # Parsing, encryption and database work are blocking: run them off the
    # event loop.
    checked = await run_in_threadpool(check_upload, data, file.filename)
    return await run_in_threadpool(
        svc.upload,
        db,
        customer_id,
        checked,
        user,
        document_type_id=document_type_id,
        job_id=job_id,
        checklist_item_id=checklist_item_id,
    )


@router.get(
    "/customers/{customer_id}/documents",
    response_model=list[DocumentRead],
    responses=NOT_FOUND,
)
def list_documents(
    customer_id: Id,
    user: CurrentUser,
    db: DbSession,
    include_archived: bool = False,
    status: DocumentStatus | None = None,
) -> list[DocumentRead]:
    return svc.list_customer_documents(
        db, customer_id, user, include_archived=include_archived, status=status
    )


@router.get(
    "/documents/{document_id}", response_model=DocumentDetail, responses=NOT_FOUND
)
def get_document(document_id: Id, user: CurrentUser, db: DbSession) -> DocumentDetail:
    return svc.get_document(db, document_id, user)


def _file_response(document_id: int, user, db, version: int | None, inline: bool):
    stored, data = svc.read_file(db, document_id, user, version, inline)
    disposition = "inline" if inline else "attachment"
    filename = stored.original_filename.replace('"', "")
    return Response(
        content=data,
        media_type=stored.content_type,
        headers={
            "Content-Disposition": f'{disposition}; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


FILE_RESPONSES = {
    **NOT_FOUND,
    200: {"content": {"application/octet-stream": {}}, "description": "The file"},
}
Version = Annotated[int | None, Query(gt=0, le=10_000)]


@router.get("/documents/{document_id}/download", responses=FILE_RESPONSES)
def download(
    document_id: Id, user: CurrentUser, db: DbSession, version: Version = None
):
    return _file_response(document_id, user, db, version, inline=False)


@router.get("/documents/{document_id}/preview", responses=FILE_RESPONSES)
def preview(document_id: Id, user: CurrentUser, db: DbSession, version: Version = None):
    return _file_response(document_id, user, db, version, inline=True)


@router.post(
    "/documents/{document_id}/versions",
    response_model=DocumentDetail,
    status_code=status.HTTP_201_CREATED,
    responses=UPLOAD,
)
async def add_version(
    document_id: Id,
    user: CurrentUser,
    db: DbSession,
    file: Annotated[UploadFile, File(description="PDF, PNG or JPEG, up to 10 MB")],
    change_reason: Annotated[str, Form(min_length=1, max_length=1000)],
) -> DocumentDetail:
    data = await _read_limited(file)
    checked = await run_in_threadpool(check_upload, data, file.filename)
    return await run_in_threadpool(
        svc.add_version, db, document_id, checked, change_reason.strip(), user
    )


@router.post(
    "/documents/{document_id}/verify", response_model=DocumentDetail, responses=WRITE
)
def verify(document_id: Id, user: CurrentUser, db: DbSession) -> DocumentDetail:
    return svc.verify(db, document_id, user)


@router.post(
    "/documents/{document_id}/reject", response_model=DocumentDetail, responses=WRITE
)
def reject(
    document_id: Id, payload: RejectRequest, user: CurrentUser, db: DbSession
) -> DocumentDetail:
    return svc.reject(db, document_id, payload.reason, user)


@router.post(
    "/documents/{document_id}/archive", response_model=DocumentDetail, responses=WRITE
)
def archive(
    document_id: Id, payload: ArchiveRequest, admin: AdminUser, db: DbSession
) -> DocumentDetail:
    return svc.archive(db, document_id, payload.reason, admin)


@router.get(
    "/jobs/{job_id}/checklist",
    response_model=ChecklistResponse,
    responses=JOB_NOT_FOUND,
)
def get_checklist(job_id: JobId, user: CurrentUser, db: DbSession) -> ChecklistResponse:
    return svc.get_checklist(db, job_id, user)


@router.post(
    "/jobs/{job_id}/checklist/{item_id}/documents",
    response_model=ChecklistResponse,
    status_code=status.HTTP_201_CREATED,
    responses=WRITE,
)
def link_document(
    job_id: JobId,
    item_id: Id,
    payload: LinkDocumentRequest,
    user: CurrentUser,
    db: DbSession,
) -> ChecklistResponse:
    return svc.link_document(db, job_id, item_id, payload.document_id, user)
