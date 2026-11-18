import math
from datetime import date

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Job
from app.repositories import job_repository as repo
from app.repositories.job_repository import JobRow
from app.schemas.job import (
    CustomerRef,
    JobCreate,
    JobListResponse,
    JobRead,
    JobStatus,
    JobUpdate,
    ServiceRef,
)
from app.services.errors import BusinessRuleError, ConflictError, NotFoundError

JOB_NUMBER_SEQUENCE = "job_number"
JOB_NUMBER_PREFIX = "CSC"
# The job-number year follows the centre's local calendar, not UTC.
BUSINESS_TIMEZONE = "Asia/Kolkata"


def format_job_number(year: int, value: int) -> str:
    return f"{JOB_NUMBER_PREFIX}-{year}-{value:06d}"


def _to_read(row: JobRow) -> JobRead:
    job, customer, service = row
    return JobRead(
        id=job.id,
        job_number=job.job_number,
        title=job.title,
        description=job.description,
        status=job.status,
        due_date=job.due_date,
        created_by=job.created_by,
        created_at=job.created_at,
        customer=CustomerRef.model_validate(customer),
        service=ServiceRef.model_validate(service) if service is not None else None,
    )


def get_job(db: Session, job_id: int) -> JobRead:
    row = repo.get_job_with_relations(db, job_id)
    if row is None:
        raise NotFoundError("Job not found.")
    return _to_read(row)


def create_job(db: Session, data: JobCreate, created_by: int | None = None) -> JobRead:
    """Create a job with a server-generated number and status ``pending``.

    ``created_by`` must come from the authenticated user, never the request
    body. Authentication is not implemented yet, so routes pass ``None``.
    """
    if repo.get_customer(db, data.customer_id) is None:
        raise NotFoundError("Customer not found.")
    service = repo.get_service(db, data.service_id)
    if service is None:
        raise NotFoundError("Service not found.")
    if not service.is_active:
        raise BusinessRuleError("Service is inactive and cannot be used for new jobs.")

    try:
        year = repo.current_year(db, BUSINESS_TIMEZONE)
        value = repo.next_sequence_value(db, JOB_NUMBER_SEQUENCE, year)
        job = repo.add_job(
            db,
            Job(
                job_number=format_job_number(year, value),
                customer_id=data.customer_id,
                service_id=data.service_id,
                title=data.title,
                description=data.description,
                due_date=data.due_date,
                status=JobStatus.PENDING.value,
                created_by=created_by,
            ),
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ConflictError("The job could not be created. Please retry.") from None

    return get_job(db, job.id)


def list_jobs(
    db: Session,
    *,
    page: int,
    page_size: int,
    search: str | None = None,
    customer_id: int | None = None,
    service_id: int | None = None,
    status: JobStatus | None = None,
    due_date: date | None = None,
) -> JobListResponse:
    search = search.strip() if search else None
    rows, total = repo.list_jobs(
        db,
        offset=(page - 1) * page_size,
        limit=page_size,
        search=search or None,
        customer_id=customer_id,
        service_id=service_id,
        status=status.value if status is not None else None,
        due_date=due_date,
    )
    return JobListResponse(
        items=[_to_read(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size) if total else 0,
    )


def update_job(db: Session, job_id: int, data: JobUpdate) -> JobRead:
    job = repo.get_job(db, job_id)
    if job is None:
        raise NotFoundError("Job not found.")

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(job, field, value)
    db.commit()

    return get_job(db, job_id)
