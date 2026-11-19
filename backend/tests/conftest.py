"""Test fixtures.

Database tests run only against the database named by TEST_DATABASE_URL, whose
name must end in ``_test``. They never fall back to DATABASE_URL, so the
developer database is never touched. Local setup uses a test-only role with a
throwaway password (not a real credential) on the compose Postgres:

    TEST_DATABASE_URL=postgresql+psycopg://csc_test:csc_test_only@localhost:5433/csc_crm_test

Each test runs inside a transaction that is rolled back afterwards; tests that
need real commits (concurrency) clean up with TRUNCATE on the test database.
"""

import os
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, make_url
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.main import app
from app.models import Customer, Service, ServiceCategory

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _test_database_url() -> str:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.fail(
            "TEST_DATABASE_URL is not set. Database tests need an isolated "
            "PostgreSQL database whose name ends in '_test'.",
            pytrace=False,
        )
    database = make_url(url).database or ""
    if not database.endswith("_test"):
        pytest.fail(
            f"Refusing to run tests against database {database!r}: "
            "its name must end in '_test'.",
            pytrace=False,
        )
    return url


@pytest.fixture(scope="session")
def test_engine() -> Iterator[Engine]:
    url = _test_database_url()
    # migrations/env.py reads DATABASE_URL; point it at the test database only
    # for the duration of the upgrade.
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        command.upgrade(Config(str(BACKEND_DIR / "alembic.ini")), "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous

    engine = create_engine(url, pool_size=15, max_overflow=5)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(test_engine: Engine) -> Iterator[Session]:
    connection = test_engine.connect()
    transaction = connection.begin()
    session = Session(
        bind=connection,
        join_transaction_mode="create_savepoint",
        autoflush=False,
    )
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: db_session
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def make_customer(db_session: Session):
    counter = iter(range(1, 10_000))

    def _make(name: str | None = None, phone: str = "9000000000") -> Customer:
        customer = Customer(name=name or f"Test Customer {next(counter)}", phone=phone)
        db_session.add(customer)
        db_session.flush()
        return customer

    return _make


@pytest.fixture
def make_service(db_session: Session):
    counter = iter(range(1, 10_000))
    category = ServiceCategory(name="Test Category", code="TEST-CAT")
    db_session.add(category)
    db_session.flush()

    def _make(is_active: bool = True, name: str | None = None) -> Service:
        n = next(counter)
        service = Service(
            category_id=category.id,
            name=name or f"Test Service {n}",
            code=f"TEST-SVC-{n}",
            fee_type="fixed",
            default_service_fee=Decimal("100.00"),
            is_active=is_active,
        )
        db_session.add(service)
        db_session.flush()
        return service

    return _make
