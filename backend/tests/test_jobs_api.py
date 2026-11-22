import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import AuditLog, Job, SequenceCounter

JOB_NUMBER_RE = re.compile(r"^CSC-(\d{4})-(\d{6})$")


@pytest.fixture
def customer(make_customer):
    return make_customer(name="Asha Test", phone="9000000001")


@pytest.fixture
def service(make_service):
    return make_service(name="Vehicle Registration Renewal")


def create(client, customer, service, **overrides):
    body = {"customer_id": customer.id, "service_id": service.id, "title": "Job"}
    body.update(overrides)
    return client.post("/api/v1/jobs", json=body)


# ---------- create ----------


def test_create_job(client, admin_user, customer, service):
    response = create(
        client,
        customer,
        service,
        title="Vehicle Registration Renewal",
        description="Renewal application",
        due_date="2026-11-25",
    )

    assert response.status_code == 201
    body = response.json()
    assert JOB_NUMBER_RE.match(body["job_number"])
    assert body["status"] == "pending"
    assert body["title"] == "Vehicle Registration Renewal"
    assert body["description"] == "Renewal application"
    assert body["due_date"] == "2026-11-25"
    assert body["created_by"] == admin_user.id
    assert body["customer"] == {
        "id": customer.id,
        "name": "Asha Test",
        "phone": "9000000001",
    }
    assert body["service"]["id"] == service.id


def test_create_job_numbers_are_sequential(client, db_session, customer, service):
    first = create(client, customer, service).json()["job_number"]
    second = create(client, customer, service).json()["job_number"]

    year1, n1 = JOB_NUMBER_RE.match(first).groups()
    year2, n2 = JOB_NUMBER_RE.match(second).groups()
    assert year1 == year2
    assert int(n2) == int(n1) + 1
    counter = db_session.get(SequenceCounter, ("job_number", int(year1)))
    assert counter.last_value == int(n2)


def test_create_job_optional_fields_default_to_null(client, customer, service):
    response = create(client, customer, service)

    assert response.status_code == 201
    assert response.json()["description"] is None
    assert response.json()["due_date"] is None


def test_create_job_unknown_customer(client, service):
    response = client.post(
        "/api/v1/jobs",
        json={"customer_id": 999999, "service_id": service.id, "title": "X"},
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "Customer not found."}


def test_create_job_unknown_service(client, customer):
    response = client.post(
        "/api/v1/jobs",
        json={"customer_id": customer.id, "service_id": 999999, "title": "X"},
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "Service not found."}


def test_create_job_inactive_service(client, customer, make_service):
    inactive = make_service(is_active=False)
    response = create(client, customer, inactive)
    assert response.status_code == 422
    assert "inactive" in response.json()["detail"]


@pytest.mark.parametrize("missing", ["customer_id", "service_id", "title"])
def test_create_job_required_fields(client, customer, service, missing):
    body = {"customer_id": customer.id, "service_id": service.id, "title": "X"}
    del body[missing]
    assert client.post("/api/v1/jobs", json=body).status_code == 422


@pytest.mark.parametrize("title", ["", "   ", "x" * 201])
def test_create_job_invalid_title(client, customer, service, title):
    assert create(client, customer, service, title=title).status_code == 422


@pytest.mark.parametrize(
    "field, value",
    [
        ("job_number", "CSC-2026-999999"),
        ("status", "completed"),
        ("created_by", 1),
        ("created_at", "2020-01-01T00:00:00Z"),
        ("id", 12345),
    ],
)
def test_create_job_rejects_protected_fields(
    client, db_session, customer, service, field, value
):
    response = create(client, customer, service, **{field: value})
    assert response.status_code == 422
    assert db_session.scalar(select(Job).where(Job.customer_id == customer.id)) is None


# ---------- list ----------


def test_list_jobs(client, customer, service):
    create(client, customer, service, title="A")
    create(client, customer, service, title="B")

    body = client.get("/api/v1/jobs", params={"customer_id": customer.id}).json()
    assert body["total"] == 2
    assert body["page"] == 1
    assert body["page_size"] == 20
    assert body["total_pages"] == 1
    assert {j["title"] for j in body["items"]} == {"A", "B"}
    assert body["items"][0]["customer"]["id"] == customer.id


def test_list_jobs_pagination(client, customer, service):
    for i in range(5):
        create(client, customer, service, title=f"Job {i}")

    params = {"customer_id": customer.id, "page_size": 2}
    page1 = client.get("/api/v1/jobs", params={**params, "page": 1}).json()
    page3 = client.get("/api/v1/jobs", params={**params, "page": 3}).json()
    page4 = client.get("/api/v1/jobs", params={**params, "page": 4}).json()

    assert page1["total"] == 5
    assert page1["total_pages"] == 3
    assert len(page1["items"]) == 2
    assert len(page3["items"]) == 1
    assert page4["items"] == []
    all_ids = [
        j["id"]
        for p in (1, 2, 3)
        for j in client.get("/api/v1/jobs", params={**params, "page": p}).json()[
            "items"
        ]
    ]
    assert len(set(all_ids)) == 5


@pytest.mark.parametrize(
    "params",
    [{"page_size": 101}, {"page_size": 0}, {"page": 0}, {"status": "unknown"}],
)
def test_list_jobs_invalid_params(client, params):
    assert client.get("/api/v1/jobs", params=params).status_code == 422


def test_list_jobs_max_page_size_allowed(client):
    assert client.get("/api/v1/jobs", params={"page_size": 100}).status_code == 200


def test_list_jobs_search_by_job_number(client, customer, service):
    target = create(client, customer, service, title="Alpha").json()
    create(client, customer, service, title="Beta")

    body = client.get("/api/v1/jobs", params={"search": target["job_number"]}).json()
    assert [j["id"] for j in body["items"]] == [target["id"]]


def test_list_jobs_search_by_title_case_insensitive(client, customer, service):
    target = create(client, customer, service, title="PAN Card Correction").json()
    create(client, customer, service, title="Passport")

    body = client.get(
        "/api/v1/jobs", params={"search": "card corr", "customer_id": customer.id}
    ).json()
    assert [j["id"] for j in body["items"]] == [target["id"]]


def test_list_jobs_search_treats_wildcards_literally(client, customer, service):
    create(client, customer, service, title="Plain title")
    target = create(client, customer, service, title="100% refund").json()

    body = client.get(
        "/api/v1/jobs", params={"search": "%", "customer_id": customer.id}
    ).json()
    assert [j["id"] for j in body["items"]] == [target["id"]]


def test_list_jobs_filter_by_customer(client, make_customer, service):
    c1, c2 = make_customer(), make_customer()
    job = create(client, c1, service).json()
    create(client, c2, service)

    body = client.get("/api/v1/jobs", params={"customer_id": c1.id}).json()
    assert [j["id"] for j in body["items"]] == [job["id"]]


def test_list_jobs_filter_by_service(client, customer, make_service):
    s1, s2 = make_service(), make_service()
    job = create(client, customer, s1).json()
    create(client, customer, s2)

    body = client.get("/api/v1/jobs", params={"service_id": s1.id}).json()
    assert [j["id"] for j in body["items"]] == [job["id"]]


def test_list_jobs_filter_by_status(client, db_session, customer, service):
    pending = create(client, customer, service).json()
    other = create(client, customer, service).json()
    db_session.get(Job, other["id"]).status = "completed"
    db_session.flush()

    params = {"customer_id": customer.id}
    pending_ids = [
        j["id"]
        for j in client.get(
            "/api/v1/jobs", params={**params, "status": "pending"}
        ).json()["items"]
    ]
    completed_ids = [
        j["id"]
        for j in client.get(
            "/api/v1/jobs", params={**params, "status": "completed"}
        ).json()["items"]
    ]
    assert pending_ids == [pending["id"]]
    assert completed_ids == [other["id"]]


def test_list_jobs_filter_by_due_date(client, customer, service):
    job = create(client, customer, service, due_date="2026-12-01").json()
    create(client, customer, service, due_date="2026-12-02")
    create(client, customer, service)

    body = client.get(
        "/api/v1/jobs", params={"due_date": "2026-12-01", "customer_id": customer.id}
    ).json()
    assert [j["id"] for j in body["items"]] == [job["id"]]


def test_list_jobs_newest_first(client, db_session, customer, service):
    # Insert with explicit timestamps so ordering by created_at is observable
    # (rows created in one transaction otherwise share the same now()).
    base = datetime(2026, 1, 1, tzinfo=UTC)
    jobs = [
        Job(
            job_number=f"TEST-ORDER-{i}",
            customer_id=customer.id,
            service_id=service.id,
            title=f"Order {i}",
            created_at=base + timedelta(days=offset),
        )
        for i, offset in enumerate([2, 0, 1])
    ]
    db_session.add_all(jobs)
    db_session.flush()

    body = client.get("/api/v1/jobs", params={"customer_id": customer.id}).json()
    assert [j["title"] for j in body["items"]] == ["Order 0", "Order 2", "Order 1"]


# ---------- get ----------


def test_get_job(client, customer, service):
    created = create(
        client, customer, service, title="Get me", due_date="2026-11-30"
    ).json()

    response = client.get(f"/api/v1/jobs/{created['id']}")

    assert response.status_code == 200
    body = response.json()
    assert body == created
    assert set(body) == {
        "id",
        "job_number",
        "title",
        "description",
        "status",
        "due_date",
        "created_by",
        "created_at",
        "customer",
        "service",
        "assignees",
        "allowed_next_statuses",
        "document_progress",
        "payment_summary",
    }
    assert body["customer"] == {
        "id": customer.id,
        "name": "Asha Test",
        "phone": "9000000001",
    }
    assert body["service"] == {
        "id": service.id,
        "name": service.name,
        "code": service.code,
    }


def test_get_job_not_found(client):
    response = client.get("/api/v1/jobs/999999")
    assert response.status_code == 404
    assert response.json() == {"detail": "Job not found."}


# ---------- update ----------


@pytest.fixture
def job(client, customer, service):
    return create(
        client,
        customer,
        service,
        title="Original",
        description="Desc",
        due_date="2026-11-25",
    ).json()


def test_update_title_preserves_other_fields(client, job):
    response = client.patch(f"/api/v1/jobs/{job['id']}", json={"title": "Renamed"})

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Renamed"
    assert body["description"] == "Desc"
    assert body["due_date"] == "2026-11-25"
    assert body["job_number"] == job["job_number"]


def test_update_description(client, job):
    body = client.patch(f"/api/v1/jobs/{job['id']}", json={"description": "New"}).json()
    assert body["description"] == "New"
    assert body["title"] == "Original"


def test_update_due_date(client, job):
    body = client.patch(
        f"/api/v1/jobs/{job['id']}", json={"due_date": "2026-12-31"}
    ).json()
    assert body["due_date"] == "2026-12-31"
    assert body["title"] == "Original"


def test_update_can_clear_optional_fields(client, job):
    body = client.patch(
        f"/api/v1/jobs/{job['id']}", json={"description": None, "due_date": None}
    ).json()
    assert body["description"] is None
    assert body["due_date"] is None


def test_update_persists(client, job):
    client.patch(f"/api/v1/jobs/{job['id']}", json={"title": "Saved"})
    assert client.get(f"/api/v1/jobs/{job['id']}").json()["title"] == "Saved"


@pytest.mark.parametrize("title", [None, "", "x" * 201])
def test_update_invalid_title(client, job, title):
    response = client.patch(f"/api/v1/jobs/{job['id']}", json={"title": title})
    assert response.status_code == 422


def test_update_not_found(client):
    response = client.patch("/api/v1/jobs/999999", json={"title": "X"})
    assert response.status_code == 404


@pytest.mark.parametrize(
    "field, value",
    [
        ("id", 999),
        ("job_number", "CSC-2026-999999"),
        ("customer_id", 1),
        ("service_id", 1),
        ("status", "completed"),
        ("created_by", 1),
        ("created_at", "2020-01-01T00:00:00Z"),
    ],
)
def test_update_rejects_protected_fields(client, job, field, value):
    response = client.patch(
        f"/api/v1/jobs/{job['id']}", json={field: value, "title": "X"}
    )

    assert response.status_code == 422
    assert client.get(f"/api/v1/jobs/{job['id']}").json() == job


# ---------- bounds (review finding M2 / L1) ----------

TOO_BIG = 2**31


@pytest.mark.parametrize(
    "method, url",
    [("get", f"/api/v1/jobs/{TOO_BIG}"), ("patch", f"/api/v1/jobs/{TOO_BIG}")],
)
def test_out_of_range_job_id_is_422(client, method, url):
    kwargs = {"json": {"title": "X"}} if method == "patch" else {}
    assert getattr(client, method)(url, **kwargs).status_code == 422


@pytest.mark.parametrize(
    "params",
    [
        {"customer_id": TOO_BIG},
        {"service_id": TOO_BIG},
        {"page": 10**17, "page_size": 100},
    ],
)
def test_out_of_range_list_params_are_422(client, params):
    assert client.get("/api/v1/jobs", params=params).status_code == 422


@pytest.mark.parametrize("field", ["customer_id", "service_id"])
def test_create_out_of_range_ids_are_422(client, customer, service, field):
    assert create(client, customer, service, **{field: TOO_BIG}).status_code == 422


def test_description_length_limit(client, customer, service, job):
    assert create(client, customer, service, description="x" * 5000).status_code == 201
    assert create(client, customer, service, description="x" * 5001).status_code == 422
    response = client.patch(
        f"/api/v1/jobs/{job['id']}", json={"description": "x" * 5001}
    )
    assert response.status_code == 422


# ---------- access control (auth step 1.0) ----------


def test_jobs_require_sign_in(anon_client, job):
    assert anon_client.get("/api/v1/jobs").status_code == 401
    assert anon_client.get(f"/api/v1/jobs/{job['id']}").status_code == 401
    assert anon_client.post("/api/v1/jobs", json={}).status_code == 401
    response = anon_client.patch(f"/api/v1/jobs/{job['id']}", json={"title": "X"})
    assert response.status_code == 401


def test_employee_creating_a_job_is_assigned_to_it(
    employee_client, employee_user, customer, service
):
    response = create(employee_client, customer, service, title="By employee")

    assert response.status_code == 201
    body = response.json()
    assert body["created_by"] == employee_user.id
    assert body["assignees"] == [{"id": employee_user.id, "name": employee_user.name}]
    assert employee_client.get(f"/api/v1/jobs/{body['id']}").status_code == 200


def test_admin_creating_a_job_is_not_auto_assigned(client, customer, service):
    assert create(client, customer, service).json()["assignees"] == []


def test_employee_does_not_see_unassigned_jobs(employee_client, job):
    assert employee_client.get("/api/v1/jobs").json()["total"] == 0
    response = employee_client.get(f"/api/v1/jobs/{job['id']}")
    assert response.status_code == 404
    assert response.json() == {"detail": "Job not found."}


def test_employee_cannot_edit_unassigned_jobs(employee_client, job):
    response = employee_client.patch(f"/api/v1/jobs/{job['id']}", json={"title": "X"})
    assert response.status_code == 404


def test_employee_can_edit_assigned_jobs(
    client, employee_client, employee_user, job, db_session
):
    client.post(
        f"/api/v1/jobs/{job['id']}/assignments", json={"user_id": employee_user.id}
    )

    response = employee_client.patch(
        f"/api/v1/jobs/{job['id']}", json={"title": "Edited by employee"}
    )

    assert response.status_code == 200
    assert response.json()["title"] == "Edited by employee"
    entry = db_session.scalar(
        select(AuditLog)
        .where(AuditLog.action == "job_updated", AuditLog.entity_id == job["id"])
        .order_by(AuditLog.id.desc())
    )
    assert entry.user_id == employee_user.id


def test_job_write_requires_csrf(client, customer, service):
    del client.headers["X-CSRF-Token"]
    assert create(client, customer, service).status_code == 403


def test_job_changes_are_audited(client, admin_user, db_session, job):
    client.patch(f"/api/v1/jobs/{job['id']}", json={"title": "Audited"})

    entries = db_session.scalars(
        select(AuditLog)
        .where(
            AuditLog.entity_type == "job",
            AuditLog.entity_id == job["id"],
            AuditLog.action.like("job_%"),
        )
        .order_by(AuditLog.id)
    ).all()
    assert [e.action for e in entries] == ["job_created", "job_updated"]
    assert all(e.user_id == admin_user.id for e in entries)
    assert '"title"' in entries[1].details


def test_unchanged_patch_writes_no_audit(client, db_session, job):
    client.patch(f"/api/v1/jobs/{job['id']}", json={"title": job["title"]})
    actions = db_session.scalars(
        select(AuditLog.action).where(
            AuditLog.entity_id == job["id"], AuditLog.action.like("job_%")
        )
    ).all()
    assert actions == ["job_created"]
