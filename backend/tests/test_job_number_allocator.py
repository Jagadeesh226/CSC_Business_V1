import re
import threading
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import Customer, Job, SequenceCounter, Service, ServiceCategory
from app.repositories import job_repository
from app.schemas.job import JobCreate
from app.services import job_service


def test_format_job_number():
    assert job_service.format_job_number(2026, 1) == "CSC-2026-000001"
    assert job_service.format_job_number(2026, 123456) == "CSC-2026-123456"


def test_counter_increments_and_resets_per_year(db_session):
    allocate = job_repository.next_sequence_value
    assert allocate(db_session, "test_seq", 2030) == 1
    assert allocate(db_session, "test_seq", 2030) == 2
    assert allocate(db_session, "test_seq", 2031) == 1
    assert allocate(db_session, "test_seq", 2030) == 3
    assert allocate(db_session, "other_seq", 2030) == 1


def test_rolled_back_allocation_is_not_consumed(test_engine):
    with Session(test_engine) as session:
        first = job_repository.next_sequence_value(session, "test_rollback", 2030)
        session.rollback()
        second = job_repository.next_sequence_value(session, "test_rollback", 2030)
        session.rollback()
    assert first == second == 1


@pytest.fixture
def committed_fixtures(test_engine):
    """Committed rows (visible to other connections); truncated before and after."""
    _truncate(test_engine)
    with Session(test_engine) as session:
        category = ServiceCategory(name="Concurrency Category", code="CONC-CAT")
        session.add(category)
        session.flush()
        service = Service(
            category_id=category.id,
            name="Concurrency Service",
            code="CONC-SVC",
            fee_type="fixed",
            default_service_fee=Decimal("50.00"),
        )
        customer = Customer(name="Concurrency Customer", phone="9000000099")
        session.add_all([service, customer])
        session.commit()
        ids = customer.id, service.id
    yield ids
    _truncate(test_engine)


def _truncate(engine) -> None:
    # Test database only (guarded in conftest).
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE audit_logs, jobs, sequence_counters, customers, services, "
                "service_categories RESTART IDENTITY CASCADE"
            )
        )


def test_concurrent_job_creation_gets_unique_contiguous_numbers(
    test_engine, committed_fixtures
):
    customer_id, service_id = committed_fixtures
    workers = 12
    barrier = threading.Barrier(workers)
    numbers: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        try:
            with Session(test_engine) as session:
                barrier.wait(timeout=10)
                job = job_service.create_job(
                    session,
                    JobCreate(
                        customer_id=customer_id, service_id=service_id, title=f"T{i}"
                    ),
                )
            with lock:
                numbers.append(job.job_number)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert errors == []
    assert len(numbers) == workers
    assert len(set(numbers)) == workers
    parsed = [re.fullmatch(r"CSC-(\d{4})-(\d{6})", n).groups() for n in numbers]
    assert len({year for year, _ in parsed}) == 1
    assert sorted(int(seq) for _, seq in parsed) == list(range(1, workers + 1))


@pytest.mark.parametrize(
    "moment, expected",
    [
        # 18:29 UTC on 31 Dec is 23:59 IST: still the old year.
        (datetime(2026, 12, 31, 18, 29, tzinfo=UTC), 2026),
        # 18:30 UTC on 31 Dec is 00:00 IST on 1 Jan: the new year.
        (datetime(2026, 12, 31, 18, 30, tzinfo=UTC), 2027),
    ],
)
def test_job_number_year_follows_business_timezone(db_session, moment, expected):
    year = job_repository.current_year(
        db_session, job_service.BUSINESS_TIMEZONE, moment
    )
    assert year == expected


def test_create_recovers_from_numbers_the_counter_did_not_issue(
    test_engine, committed_fixtures
):
    """A pre-existing job number must not block job creation (review M1)."""
    customer_id, service_id = committed_fixtures
    with Session(test_engine) as session:
        year = job_repository.current_year(session, job_service.BUSINESS_TIMEZONE)
        # Legacy/imported jobs the counter knows nothing about.
        for n in (1, 2, 5):
            session.add(
                Job(
                    job_number=job_service.format_job_number(year, n),
                    customer_id=customer_id,
                    service_id=service_id,
                    title=f"Legacy {n}",
                )
            )
        session.commit()

        data = JobCreate(customer_id=customer_id, service_id=service_id, title="New")
        first = job_service.create_job(session, data)
        second = job_service.create_job(session, data)
        counter = session.get(SequenceCounter, ("job_number", year))

    assert first.job_number == job_service.format_job_number(year, 6)
    assert second.job_number == job_service.format_job_number(year, 7)
    assert counter.last_value == 7
