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

# Test clients talk plain HTTP, so auth cookies must not be Secure-only.
os.environ["COOKIE_SECURE"] = "false"
# Tests use the fake OCR provider, which reads answers embedded in test files.
os.environ["OCR_PROVIDER"] = "fake"

from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, make_url
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.db.session import get_db
from app.main import app
from app.models import Customer, Service, ServiceCategory, User

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


@pytest.fixture(scope="session", autouse=True)
def document_storage(tmp_path_factory) -> Path:
    """Encrypted document storage in a throwaway directory with a throwaway key."""
    from app.core.storage import generate_key

    root = tmp_path_factory.mktemp("documents")
    os.environ["DOCUMENT_STORAGE_DIR"] = str(root)
    os.environ["DOCUMENT_ENCRYPTION_KEY"] = generate_key()
    return root


@pytest.fixture(autouse=True)
def fake_redis():
    """Workers' wake-up channel, in memory: tests never touch a real Redis."""
    import fakeredis

    from app.workers import notifier

    client = fakeredis.FakeRedis()
    notifier.set_redis_factory(lambda: client)
    try:
        yield client
    finally:
        notifier.set_redis_factory(None)


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


TEST_PASSWORD = "correct-horse-battery"
# Hashed once: Argon2 is deliberately slow.
_TEST_PASSWORD_HASH = hash_password(TEST_PASSWORD)


@pytest.fixture
def make_user(db_session: Session) -> Callable[..., User]:
    counter = iter(range(1, 10_000))

    def _make(
        role: str = "employee",
        *,
        name: str | None = None,
        email: str | None = None,
        is_active: bool = True,
        must_change_password: bool = False,
    ) -> User:
        n = next(counter)
        user = User(
            name=name or f"Test {role.title()} {n}",
            email=email or f"{role}{n}@example.com",
            password_hash=_TEST_PASSWORD_HASH,
            role=role,
            is_active=is_active,
            must_change_password=must_change_password,
        )
        db_session.add(user)
        db_session.flush()
        return user

    return _make


@pytest.fixture
def make_client(db_session: Session) -> Iterator[Callable[..., TestClient]]:
    """TestClient factory; pass a user to get a signed-in client with CSRF set."""
    app.dependency_overrides[get_db] = lambda: db_session

    def _make(user: User | None = None, password: str = TEST_PASSWORD) -> TestClient:
        test_client = TestClient(app)
        if user is not None:
            response = test_client.post(
                "/api/v1/auth/login",
                json={"email": user.email, "password": password},
            )
            assert response.status_code == 200, response.text
            test_client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        return test_client

    try:
        yield _make
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def admin_user(make_user) -> User:
    return make_user("admin", name="Admin Test")


@pytest.fixture
def employee_user(make_user) -> User:
    return make_user("employee", name="Employee Test")


@pytest.fixture
def client(make_client, admin_user) -> TestClient:
    """Signed-in admin client (full access)."""
    return make_client(admin_user)


@pytest.fixture
def employee_client(make_client, employee_user) -> TestClient:
    return make_client(employee_user)


@pytest.fixture
def anon_client(make_client) -> TestClient:
    return make_client()


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
