import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.security import hash_token
from app.models import AuditLog, UserSession
from tests.conftest import TEST_PASSWORD

LOGIN = "/api/v1/auth/login"
ME = "/api/v1/auth/me"


def audit_actions(db_session, entity_id=None):
    stmt = select(AuditLog.action).order_by(AuditLog.id)
    if entity_id is not None:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    return list(db_session.scalars(stmt))


# ---------- login ----------


def test_login_sets_cookies_and_returns_user(anon_client, employee_user, db_session):
    response = anon_client.post(
        LOGIN, json={"email": employee_user.email, "password": TEST_PASSWORD}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["user"]["id"] == employee_user.id
    assert body["user"]["role"] == "employee"
    assert "password_hash" not in body["user"]
    assert body["csrf_token"]

    set_cookie = response.headers.get_list("set-cookie")
    session_cookie = next(c for c in set_cookie if c.startswith("csc_session="))
    csrf_cookie = next(c for c in set_cookie if c.startswith("csc_csrf="))
    assert "HttpOnly" in session_cookie
    assert "samesite=lax" in session_cookie.lower()
    assert "HttpOnly" not in csrf_cookie

    # Only a hash of the token is stored.
    token = anon_client.cookies.get("csc_session")
    stored = db_session.scalar(
        select(UserSession).where(UserSession.user_id == employee_user.id)
    )
    assert stored.token_hash == hash_token(token)
    assert stored.token_hash != token
    assert "login_succeeded" in audit_actions(db_session, employee_user.id)


def test_login_email_is_case_insensitive(anon_client, employee_user):
    response = anon_client.post(
        LOGIN, json={"email": employee_user.email.upper(), "password": TEST_PASSWORD}
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    "email, password",
    [("nobody@example.com", TEST_PASSWORD), (None, "wrong-password-123")],
)
def test_login_failures_are_generic(
    anon_client, employee_user, db_session, email, password
):
    response = anon_client.post(
        LOGIN, json={"email": email or employee_user.email, "password": password}
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid email or password."}
    assert "csc_session" not in anon_client.cookies
    assert "login_failed" in audit_actions(db_session)


def test_login_inactive_user_fails(anon_client, make_user, db_session):
    user = make_user(is_active=False)
    response = anon_client.post(
        LOGIN, json={"email": user.email, "password": TEST_PASSWORD}
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid email or password."}


def test_login_rejects_unknown_fields(anon_client, employee_user):
    response = anon_client.post(
        LOGIN,
        json={"email": employee_user.email, "password": TEST_PASSWORD, "role": "admin"},
    )
    assert response.status_code == 422


def test_failed_login_audit_contains_no_password(
    anon_client, employee_user, db_session
):
    anon_client.post(
        LOGIN, json={"email": employee_user.email, "password": "secret-guess-999"}
    )
    details = db_session.scalars(select(AuditLog.details)).all()
    assert all("secret-guess-999" not in (d or "") for d in details)


# ---------- sessions ----------


def test_me_requires_session(anon_client):
    assert anon_client.get(ME).status_code == 401


def test_me_returns_current_user(employee_client, employee_user):
    response = employee_client.get(ME)
    assert response.status_code == 200
    assert response.json()["email"] == employee_user.email


def test_invalid_session_token_is_rejected(anon_client):
    anon_client.cookies.set("csc_session", "not-a-real-token")
    assert anon_client.get(ME).status_code == 401


def test_expired_session_is_rejected(employee_client, employee_user, db_session):
    session = db_session.scalar(
        select(UserSession).where(UserSession.user_id == employee_user.id)
    )
    session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.flush()

    assert employee_client.get(ME).status_code == 401


def test_logout_revokes_session(employee_client, employee_user, db_session):
    token = employee_client.cookies.get("csc_session")

    response = employee_client.post("/api/v1/auth/logout")

    assert response.status_code == 204
    stored = db_session.scalar(
        select(UserSession).where(UserSession.token_hash == hash_token(token))
    )
    assert stored.revoked_at is not None
    # Even if the old cookie is replayed, it no longer works.
    employee_client.cookies.set("csc_session", token)
    assert employee_client.get(ME).status_code == 401
    assert "logout" in audit_actions(db_session, employee_user.id)


def test_deactivated_user_session_stops_working(
    employee_client, employee_user, db_session
):
    employee_user.is_active = False
    db_session.flush()
    assert employee_client.get(ME).status_code == 401


# ---------- CSRF ----------


def test_state_changing_request_without_csrf_is_rejected(employee_client):
    del employee_client.headers["X-CSRF-Token"]
    response = employee_client.post("/api/v1/auth/logout")
    assert response.status_code == 403
    assert response.json() == {"detail": "Missing or invalid CSRF token."}


def test_state_changing_request_with_wrong_csrf_is_rejected(employee_client):
    employee_client.headers["X-CSRF-Token"] = "forged-token"
    assert employee_client.post("/api/v1/auth/logout").status_code == 403


def test_csrf_token_of_another_session_is_rejected(
    make_client, employee_user, admin_user
):
    employee = make_client(employee_user)
    admin = make_client(admin_user)
    employee.headers["X-CSRF-Token"] = admin.headers["X-CSRF-Token"]
    assert employee.post("/api/v1/auth/logout").status_code == 403


def test_safe_methods_do_not_need_csrf(employee_client):
    del employee_client.headers["X-CSRF-Token"]
    assert employee_client.get(ME).status_code == 200


# ---------- password change ----------

CHANGE = "/api/v1/auth/change-password"


def test_must_change_password_blocks_other_endpoints(make_client, make_user):
    user = make_user(must_change_password=True)
    client = make_client(user)

    assert client.get(ME).status_code == 200
    blocked = client.get("/api/v1/jobs")
    assert blocked.status_code == 403
    assert blocked.json() == {"detail": "You must change your password first."}

    response = client.post(
        CHANGE,
        json={"current_password": TEST_PASSWORD, "new_password": "a-brand-new-pass"},
    )
    assert response.status_code == 200
    assert response.json()["must_change_password"] is False
    assert client.get("/api/v1/jobs").status_code == 200


def test_change_password_wrong_current(employee_client):
    response = employee_client.post(
        CHANGE,
        json={
            "current_password": "not-my-password",
            "new_password": "a-brand-new-pass",
        },
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "Current password is incorrect."}


def test_change_password_must_differ(employee_client):
    response = employee_client.post(
        CHANGE, json={"current_password": TEST_PASSWORD, "new_password": TEST_PASSWORD}
    )
    assert response.status_code == 422


@pytest.mark.parametrize("new_password", ["short", "x" * 129])
def test_change_password_length_rules(employee_client, new_password):
    response = employee_client.post(
        CHANGE, json={"current_password": TEST_PASSWORD, "new_password": new_password}
    )
    assert response.status_code == 422


def test_change_password_signs_out_other_sessions(
    make_client, employee_user, db_session
):
    first = make_client(employee_user)
    second = make_client(employee_user)

    response = first.post(
        CHANGE,
        json={"current_password": TEST_PASSWORD, "new_password": "a-brand-new-pass"},
    )

    assert response.status_code == 200
    assert first.get(ME).status_code == 200
    assert second.get(ME).status_code == 401
    # The old password no longer works; the new one does.
    assert (
        make_client()
        .post(LOGIN, json={"email": employee_user.email, "password": TEST_PASSWORD})
        .status_code
        == 401
    )
    make_client(employee_user, password="a-brand-new-pass")
    assert "password_changed" in audit_actions(db_session, employee_user.id)


def test_audit_details_are_json(employee_client, db_session):
    for details in db_session.scalars(select(AuditLog.details)).all():
        if details:
            json.loads(details)
