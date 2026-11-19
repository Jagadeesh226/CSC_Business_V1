import json

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import AuditLog, User
from app.schemas.user import UserUpdate
from app.services import user_service
from app.services.errors import BusinessRuleError
from tests.conftest import TEST_PASSWORD

USERS = "/api/v1/users"
NEW_USER = {
    "name": "New Employee",
    "email": "new.employee@example.com",
    "temporary_password": "temporary-pass-1",
}


def last_audit(db_session, action):
    return db_session.scalar(
        select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id.desc())
    )


# ---------- access control ----------


@pytest.mark.parametrize(
    "method, url, body",
    [
        ("get", USERS, None),
        ("post", USERS, NEW_USER),
        ("get", f"{USERS}/1", None),
        ("patch", f"{USERS}/1", {"name": "X"}),
        ("post", f"{USERS}/1/deactivate", None),
        ("post", f"{USERS}/1/reactivate", None),
        (
            "post",
            f"{USERS}/1/reset-password",
            {"temporary_password": "temporary-pass-1"},
        ),
    ],
)
def test_user_management_requires_admin(
    anon_client, employee_client, method, url, body
):
    kwargs = {"json": body} if body is not None else {}
    assert getattr(anon_client, method)(url, **kwargs).status_code == 401
    response = getattr(employee_client, method)(url, **kwargs)
    assert response.status_code == 403
    assert response.json() == {"detail": "Admin access required."}


# ---------- create ----------


def test_create_user(client, admin_user, db_session, make_client):
    response = client.post(USERS, json=NEW_USER)

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "new.employee@example.com"
    assert body["role"] == "employee"
    assert body["must_change_password"] is True
    assert "password" not in str(body).lower().replace("must_change_password", "")
    entry = last_audit(db_session, "user_created")
    assert entry.entity_id == body["id"]
    assert entry.user_id == admin_user.id
    assert "temporary-pass-1" not in (entry.details or "")

    # The new user can sign in with the temporary password.
    new_client = make_client()
    login = new_client.post(
        "/api/v1/auth/login",
        json={"email": NEW_USER["email"], "password": NEW_USER["temporary_password"]},
    )
    assert login.status_code == 200


def test_create_admin_user(client):
    response = client.post(USERS, json={**NEW_USER, "role": "admin"})
    assert response.status_code == 201
    assert response.json()["role"] == "admin"


def test_create_user_duplicate_email_case_insensitive(client, employee_user):
    response = client.post(
        USERS, json={**NEW_USER, "email": employee_user.email.upper()}
    )
    assert response.status_code == 409


@pytest.mark.parametrize(
    "overrides",
    [
        {"role": "auditor"},
        {"role": "staff"},
        {"email": "not-an-email"},
        {"temporary_password": "short"},
        {"name": " "},
        {"is_active": False},
        {"password_hash": "x"},
    ],
)
def test_create_user_validation(client, overrides):
    assert client.post(USERS, json={**NEW_USER, **overrides}).status_code == 422


# ---------- read ----------


def test_list_and_filter_users(client, admin_user, employee_user, make_user):
    make_user(is_active=False, name="Gone Person")

    everyone = client.get(USERS).json()
    assert everyone["total"] == 3
    assert [u["id"] for u in everyone["items"]][:2] == [admin_user.id, employee_user.id]

    admins = client.get(USERS, params={"role": "admin"}).json()
    assert [u["id"] for u in admins["items"]] == [admin_user.id]

    inactive = client.get(USERS, params={"is_active": False}).json()
    assert [u["name"] for u in inactive["items"]] == ["Gone Person"]

    found = client.get(USERS, params={"search": "gone"}).json()
    assert found["total"] == 1


def test_get_user(client, employee_user):
    response = client.get(f"{USERS}/{employee_user.id}")
    assert response.status_code == 200
    assert response.json()["email"] == employee_user.email
    assert "password_hash" not in response.json()


def test_get_user_not_found(client):
    assert client.get(f"{USERS}/999999").status_code == 404
    assert client.get(f"{USERS}/{2**31}").status_code == 422


# ---------- update ----------


def test_update_user(client, employee_user, db_session):
    response = client.patch(
        f"{USERS}/{employee_user.id}",
        json={"name": "Renamed", "email": "Renamed@Example.com", "role": "admin"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Renamed"
    assert body["email"] == "renamed@example.com"
    assert body["role"] == "admin"
    assert '"role"' in last_audit(db_session, "user_updated").details


def test_update_user_email_conflict(client, employee_user, admin_user):
    response = client.patch(
        f"{USERS}/{employee_user.id}", json={"email": admin_user.email}
    )
    assert response.status_code == 409


@pytest.mark.parametrize(
    "body", [{"name": None}, {"role": None}, {"is_active": False}, {"role": "auditor"}]
)
def test_update_user_validation(client, employee_user, body):
    assert client.patch(f"{USERS}/{employee_user.id}", json=body).status_code == 422


def test_admin_cannot_remove_own_admin_role(client, admin_user):
    response = client.patch(f"{USERS}/{admin_user.id}", json={"role": "employee"})
    assert response.status_code == 422
    assert response.json() == {"detail": "You cannot remove your own admin role."}


def test_last_active_admin_cannot_be_demoted(db_session, admin_user, employee_user):
    # Through the API the acting admin always counts as another active admin,
    # so exercise the guard directly: it also protects concurrent requests.
    with pytest.raises(BusinessRuleError, match="At least one active admin"):
        user_service.update_user(
            db_session, employee_user, admin_user.id, UserUpdate(role="employee")
        )


def test_demoted_admin_loses_admin_access(client, make_user, make_client):
    other_admin = make_user("admin")
    other_client = make_client(other_admin)
    assert other_client.get(USERS).status_code == 200

    client.patch(f"{USERS}/{other_admin.id}", json={"role": "employee"})

    assert other_client.get(USERS).status_code == 403


# ---------- deactivate / reactivate ----------


def test_deactivate_and_reactivate(client, employee_user, make_client, db_session):
    employee = make_client(employee_user)

    response = client.post(f"{USERS}/{employee_user.id}/deactivate")
    assert response.status_code == 200
    assert response.json()["is_active"] is False
    assert employee.get("/api/v1/auth/me").status_code == 401
    assert (
        make_client()
        .post(
            "/api/v1/auth/login",
            json={"email": employee_user.email, "password": TEST_PASSWORD},
        )
        .status_code
        == 401
    )
    assert last_audit(db_session, "user_deactivated").entity_id == employee_user.id

    response = client.post(f"{USERS}/{employee_user.id}/reactivate")
    assert response.status_code == 200
    assert response.json()["is_active"] is True
    make_client(employee_user)  # can sign in again
    assert last_audit(db_session, "user_reactivated").entity_id == employee_user.id


def test_admin_cannot_deactivate_self(client, admin_user):
    response = client.post(f"{USERS}/{admin_user.id}/deactivate")
    assert response.status_code == 422


def test_last_active_admin_cannot_be_deactivated(db_session, admin_user, employee_user):
    with pytest.raises(BusinessRuleError, match="At least one active admin"):
        user_service.deactivate_user(db_session, employee_user, admin_user.id)


def test_deactivate_not_found(client):
    assert client.post(f"{USERS}/999999/deactivate").status_code == 404


# ---------- reset password ----------


def test_reset_password(client, employee_user, make_client, db_session):
    employee = make_client(employee_user)

    response = client.post(
        f"{USERS}/{employee_user.id}/reset-password",
        json={"temporary_password": "reset-temp-pass"},
    )

    assert response.status_code == 200
    assert response.json()["must_change_password"] is True
    assert employee.get("/api/v1/auth/me").status_code == 401
    fresh = make_client(employee_user, password="reset-temp-pass")
    assert fresh.get("/api/v1/jobs").status_code == 403  # must change first
    entry = last_audit(db_session, "password_reset")
    assert entry.entity_id == employee_user.id
    assert "reset-temp-pass" not in (entry.details or "")


def test_reset_password_validation(client, employee_user):
    response = client.post(
        f"{USERS}/{employee_user.id}/reset-password",
        json={"temporary_password": "short"},
    )
    assert response.status_code == 422


# ---------- CLI bootstrap ----------


def test_create_admin_service(db_session):
    user = user_service.create_admin(
        db_session, name="Owner", email="Owner@Example.com", password="owner-pass-123"
    )
    assert user.role == "admin"
    assert user.email == "owner@example.com"
    assert user.must_change_password is False
    assert last_audit(db_session, "user_created").entity_id == user.id


def test_cli_rejects_mismatched_passwords(monkeypatch, capsys):
    from app import cli

    answers = iter(["first-password-1", "second-password-2"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: next(answers))

    code = cli.main(["create-admin", "--name", "Owner", "--email", "o@example.com"])

    assert code == 1
    assert "do not match" in capsys.readouterr().err


# ---------- review fixes ----------


def test_role_and_email_changes_record_old_and_new(client, employee_user, db_session):
    old_email = employee_user.email
    client.patch(
        f"{USERS}/{employee_user.id}",
        json={"role": "admin", "email": "promoted@example.com"},
    )

    details = json.loads(last_audit(db_session, "user_updated").details)
    assert details["role"] == {"old": "employee", "new": "admin"}
    assert details["email"] == {"old": old_email, "new": "promoted@example.com"}


def test_admin_who_must_change_password_cannot_manage_users(make_client, make_user):
    admin = make_client(make_user("admin", must_change_password=True))
    response = admin.get(USERS)
    assert response.status_code == 403
    assert response.json() == {"detail": "You must change your password first."}


def test_emails_unique_case_insensitively_in_database(db_session, employee_user):
    db_session.add(
        User(
            name="Dup",
            email=employee_user.email.upper(),
            password_hash="x",
            role="employee",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


@pytest.mark.parametrize("email", ["admin@localhost", "not-an-email", ""])
def test_cli_rejects_invalid_email(monkeypatch, capsys, email):
    from app import cli

    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "owner-pass-123")
    code = cli.main(["create-admin", "--name", "Owner", "--email", email])
    assert code == 1
    assert "Invalid input" in capsys.readouterr().err


def test_cli_create_admin_end_to_end(monkeypatch, capsys, test_engine):
    """Runs the real command with a committing session (integration finding)."""
    from sqlalchemy import delete

    from app import cli

    monkeypatch.setattr(cli, "get_engine", lambda: test_engine)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "owner-pass-123")
    try:
        code = cli.main(
            ["create-admin", "--name", "Owner", "--email", " CLI.Owner@Example.com "]
        )
        assert code == 0, capsys.readouterr().err
        assert "(cli.owner@example.com)" in capsys.readouterr().out

        # Running it again reports the duplicate cleanly instead of crashing.
        assert (
            cli.main(
                ["create-admin", "--name", "Owner", "--email", "cli.owner@example.com"]
            )
            == 1
        )
        assert "already exists" in capsys.readouterr().err
    finally:
        with test_engine.begin() as conn:
            ids = select(User.id).where(User.email == "cli.owner@example.com")
            conn.execute(delete(AuditLog).where(AuditLog.entity_id.in_(ids)))
            conn.execute(delete(User).where(User.email == "cli.owner@example.com"))
