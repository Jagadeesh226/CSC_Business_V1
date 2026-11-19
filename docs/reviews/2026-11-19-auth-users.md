# Review: Phase 1.0 Authentication, users and job access control

- Branch: `feat/backend-auth`, commit `09bc8f3`
- Diff: `git diff origin/main...feat/backend-auth` (backend only, 28 files)
- Reviewer: review agent (findings only, no code changes)
- Date: 2026-11-19

## Verification performed

- `uv run pytest -q` against the test database: **133 passed**.
- `bash scripts/check.sh backend`: lock, Ruff lint, Ruff format, pytest **PASS**; contract check **FAIL (contract drift)**, which is expected (see Accepted gaps).
- Migration `a3d81f5c6e20` on the test database: downgrade -> upgrade round trip OK; `alembic check` reports no model/schema drift. With seeded rows: a `staff` user became `employee`, `entity_id ' 42'` became `42`; an `auditor` user makes the upgrade fail on `ck_users_role` and the whole migration rolls back (revision stayed at `7c4e2a91d5b3`). Probe rows were removed afterwards.
- Concurrency probe (real commits, two threads behind a barrier, 5 runs each): two admins deactivating each other, and two admins demoting each other. Every run had one success and one `At least one active admin is required.`, and one active admin remained. Probe rows were removed.
- HTTP probes (in a transaction that was rolled back): login with `text/plain` and form content types gets 422, so cross-site login CSRF with a simple request does not work. An unauthenticated request with an invalid body gets 401, not 422. An employee gets 403 for an invalid body on an admin route and for `PATCH /users/{self}` with `role=admin`. A `must_change_password` user gets 403 on `/jobs` and `/users`, and 403 on logout without CSRF. Median failed-login time was 26.8 ms for a known email and 28.0 ms for an unknown one. A failed change-password adds 0 audit rows. A second login in the same browser leaves the first session valid (200 on `/auth/me`).

## Critical

None.

## High

None.

## Medium

### M-1: Role changes are audited without the old and new role

- **File:** `backend/app/services/user_service.py:136-148`
- **What is wrong:** `user_updated` stores only `{"fields": ["role"]}`. The audit trail does not record whether a user was promoted to admin or demoted. The same applies to email changes, and email is the login identifier.
- **How to trigger:** As admin, `PATCH /api/v1/users/{id}` with `{"role": "admin"}`. The new `audit_logs` row has `details = {"fields": ["role"]}`.
- **Impact:** Root `AGENTS.md` requires auditing permission changes. From this log you cannot tell which accounts were given admin rights or when, and you cannot see that an email change redirected an account's login. This is the main record needed to investigate privilege escalation.
- **Suggested fix:** For `role` (and `email`), record `{"role": {"from": old, "to": new}}` in `details`. Keep this to non-sensitive fields; never include password hashes. Add a test that checks the recorded from/to values.

### M-2: Failed change-password attempts are not audited

- **File:** `backend/app/services/auth_service.py:119-120`
- **What is wrong:** A wrong `current_password` raises `BusinessRuleError` with no audit entry. Failed logins are audited, but this second password-verification path is not.
- **How to trigger:** With any session, repeat `POST /api/v1/auth/change-password` with a wrong `current_password`. Each attempt returns 422 and writes nothing to `audit_logs` (probe: 0 rows added).
- **Impact:** Anyone holding a stolen or unattended session (for example on a shared CSC counter PC) can guess the account password online without leaving a trace. Knowing the password lets them take over the account after the session expires. Rate limiting is deferred to Phase 6, but `decisions.md` §14 already requires auditing authentication events.
- **Suggested fix:** Record `password_change_failed` (user id only, no password material), and commit it before raising, as `login()` does. Add a test.

## Low

### L-1: Logging in again does not revoke the session the browser already holds

- **File:** `backend/app/services/auth_service.py:45-83`, `backend/app/api/v1/routes/auth.py:50-55`
- **What is wrong:** `login()` ignores any `csc_session` cookie sent with the request. The old session stays valid server-side until it expires 12 h later, even though the browser's cookie is overwritten.
- **How to trigger:** Log in twice with the same client, then replay the first token. `/auth/me` returns 200 (probe).
- **Impact:** Live sessions that nobody uses keep piling up. On shared machines a second user's login does not end the first user's session.
- **Suggested fix:** In the login route, if a valid session cookie is present, revoke that session before creating the new one. Also consider an idle timeout (now 12 h absolute), since counter PCs are shared.

### L-2: `create-admin` CLI does not validate the email the way login does

- **File:** `backend/app/cli.py:36-38`, `backend/app/services/user_service.py:37-46`
- **What is wrong:** `name` and `password` are validated, but `--email` goes into `create_admin` unchecked. It is only lowercased, not stripped or validated as `EmailStr`. `LoginRequest.email` is `EmailStr`, and it rejects `owner`, `admin@localhost` and `admin@csc.local` (verified).
- **How to trigger:** `python -m app.cli create-admin --name Owner --email admin@csc.local`. The admin is created, but every login attempt gets 422. An email with surrounding spaces is stored with the spaces and never matches.
- **Impact:** The bootstrap admin cannot sign in, and the CLI says it succeeded.
- **Suggested fix:** Validate with `TypeAdapter(EmailStr)` in the CLI (or in `create_admin`), the same way `UserCreate` does.

### L-3: Case-insensitive email uniqueness exists only in application code

- **File:** `backend/app/models/user.py:17-19`, `backend/app/repositories/user_repository.py:13-21`
- **What is wrong:** The unique index is on raw `email`, but lookups use `lower(email)`, which has no index. The migration does not normalize existing emails. If two rows differ only by case, `db.scalar` returns whichever row comes first.
- **How to trigger:** Insert `A@x.com` and `a@x.com` directly (or keep pre-existing mixed-case data), then log in as `a@x.com`.
- **Impact:** The account that signs in is not predictable. Every login and `email_taken` check does a sequential scan.
- **Suggested fix:** Use a unique functional index on `lower(email)` (new migration), or normalize existing rows to lowercase and keep the plain unique index.

### L-4: `user_sessions` rows are never purged

- **File:** `backend/app/models/user_session.py:25-28`, `backend/app/repositories/user_repository.py:96-104`
- **What is wrong:** Expired and revoked sessions stay in the table forever.
- **Impact:** The table grows without bound, and every failed attempt or re-login adds a row. This is not a correctness problem.
- **Suggested fix:** Add a periodic cleanup (worker or CLI) that deletes rows past `expires_at` or with `revoked_at` older than N days. Optionally index `expires_at`.

### L-5: Authentication audit entries carry no request context

- **File:** `backend/app/services/auth_service.py:51-58`, `backend/app/services/audit_service.py:9-31`
- **What is wrong:** `login_failed`, `login_succeeded` and `logout` record no client IP or user agent. For failed logins with an unknown email, the row contains only `reason`.
- **Impact:** Credential-stuffing or brute-force activity can be counted but not traced to a source.
- **Suggested fix:** Pass the client IP (and optionally the user agent, truncated) into `details` for authentication events. Decide with the owner whether to store the attempted email for unknown accounts.

### L-6: Test gaps and a test-hygiene leak

- **Files:** `backend/tests/conftest.py:17`, `backend/tests/test_users_api.py:176-182`, `backend/tests/test_job_number_allocator.py:62-70`, `backend/app/services/job_service.py:95-102`
- **What is wrong:**
  1. `COOKIE_SECURE=false` is forced for the whole suite, so no test checks that `Secure` is set by default.
  2. The last-admin guard is tested only single-threaded, but the test comment says it "also protects concurrent requests". The behaviour is correct (verified by probe), but no test protects it from regressions.
  3. No test checks that an admin with `must_change_password=true` is blocked from `/users`.
  4. This branch added a `job_created` audit to `create_job`, but the allocator concurrency test truncates `jobs, sequence_counters, customers, services, service_categories` and not `audit_logs`. Committed `job_created` rows build up in the test database (56 rows observed, `user_id` NULL).
- **Suggested fix:** Add a settings test with `COOKIE_SECURE` unset, a threaded last-admin test that commits real rows (like the allocator test), and a must-change admin test. Add `audit_logs` to the TRUNCATE list.

### L-7: Cookie names lack the `__Host-` prefix

- **File:** `backend/app/api/v1/routes/auth.py:16-34`, `backend/app/core/config.py:17-18`
- **What is wrong:** `csc_session` and `csc_csrf` can be overwritten by a sibling subdomain (cookie tossing). An attacker who controls one could put the victim into the attacker's session, including a matching CSRF token.
- **Impact:** Low for an internal deployment. It only matters if the CRM shares a parent domain with less-trusted hosts.
- **Suggested fix:** When `COOKIE_SECURE` is true, use `__Host-csc_session` / `__Host-csc_csrf` (requires `Secure`, `Path=/` and no `Domain`, all of which are already the case).

### L-8: Auth dependency placement and authorization layer

- **File:** `backend/app/api/deps.py:1-48`, `backend/app/api/v1/routes/jobs.py:92-96`
- **What is wrong:** `backend/AGENTS.md` lists "authentication dependencies" under `app/core/`, but they were added in a new `app/api/deps.py`. The job-edit permission is enforced only in the route (`AdminUser`), while the backend rules say record-level authorization belongs in services.
- **Impact:** None today, since role-only checks belong in dependencies anyway. When assignment-scoped employee edits arrive, the check must move into `job_service` or it will be bypassed by any other caller.
- **Suggested fix:** Confirm the placement with the owner. When job assignments land, implement the assigned-record check in `job_service.update_job` and pass in the actor.

## Accepted gaps (owner-confirmed or documented)

- **No login rate limiting or lockout** (deferred to Phase 6). When Phase 6 is planned, note that argon2-cffi 25.1 defaults to `memory_cost=65536` KiB (64 MiB) per verification (`backend/app/core/security.py:8`). Unknown emails also run a full verification. Concurrent unauthenticated logins can therefore use about 64 MiB each (about 2.5 GiB with a 40-thread pool). Rate limiting should also cap concurrent hashing.
- **Contract drift.** `docs/contracts/openapi.json`, `permissions.md` (still lists Read-only/Auditor) and `decisions.md` are not yet updated for `/auth/*`, `/users/*`, the new 401/403 responses or the role change. `check.sh` fails on contract drift until the owner PRs land.
- **Employees see all jobs** until job assignment exists. Only admins can PATCH.
- **Auditor role removed; `staff` becomes `employee`.**
- **Migration downgrade is lossy by design:** `employee` -> `staff`, `must_change_password` and `user_sessions` dropped.
- **Migration fails** on a non-integer `audit_logs.entity_id` or an unknown role instead of discarding data. This is intended, and the failure is transactional (verified).

## Checked and correct

- **Tokens:** `secrets.token_urlsafe(32)` (256 bits) for both session and CSRF. Only SHA-256 hashes are stored (`token_hash` unique and indexed). CSRF comparison uses `hmac.compare_digest`. The CSRF token is bound to the server-side session, not just double-submitted, and another session's token is rejected (tested).
- **Session lookup:** checks `revoked_at IS NULL`, `expires_at > now` (timezone-aware) and `users.is_active` on every request. Logout revokes server-side, and a replayed cookie gets 401.
- **No session fixation:** login always issues a fresh server-generated token and never adopts a client-supplied one.
- **Cookie flags:** session cookie `HttpOnly; SameSite=Lax; Path=/; Max-Age=43200`, `Secure` unless `COOKIE_SECURE=false` (default true). The CSRF cookie is readable by design.
- **CSRF coverage:** every non-GET/HEAD/OPTIONS route on `/auth/*` (except login), `/users/*` and `/jobs/*` goes through `get_auth_context`, logout included. Login CSRF with simple requests is blocked by FastAPI's JSON content-type check (probe: 422). CORS is limited to `http://localhost:5173` with credentials.
- **Enumeration:** the same generic 401 message for unknown email, wrong password and inactive account. A dummy-hash verification makes timing match (probe: 26.8 vs 28.0 ms median). Inactive accounts are told apart only in the audit `reason`.
- **Hashing:** Argon2id via argon2-cffi, rehash on login when parameters change, password length 10-128 (bounds hashing cost). The dependency is declared and justified in the commit message.
- **Privilege boundaries:** `/users/*` is admin-only (401 anonymous, 403 employee, tested for all 7 routes). `UserCreate`/`UserUpdate` are `extra="forbid"`, and `is_active`, `must_change_password` and `password_hash` cannot be mass-assigned. Admins cannot demote or deactivate themselves. A demoted admin loses access on the next request (role is read from the database each time).
- **must_change_password:** enforced in `get_current_user`, so only `/auth/me`, `/auth/logout` and `/auth/change-password` work. `require_admin` builds on `CurrentUser`, so a must-change admin is also blocked (verified by reading the code; see L-6 for the missing test).
- **Revocation:** deactivation and admin reset revoke all sessions. Password change revokes the other sessions and keeps the current one (tested).
- **Last-admin guard:** `SELECT ... FOR UPDATE` on active admin rows serializes concurrent demote/deactivate. Under READ COMMITTED, the blocked transaction re-checks updated rows and correctly sees one admin left (verified by concurrency probe, 10/10 runs).
- **Secrets:** no passwords, hashes or tokens in audit details, responses or logs (`UserRead` has no hash; tested for failed login and reset). The CLI prompts for the password with `getpass` and never echoes it.
- **Transactions:** the failed-login audit is committed before raising. `get_db` rolls back on exceptions. `IntegrityError` paths roll back and return 409.
- **Queries:** user list is paginated (`page_size <= 100`, `page <= 1_000_000`), search escapes `%`, `_` and `\`, and there are no N+1 queries (one count and one page query). Session resolution is a single join.
- **Jobs:** all four routes need sign-in, `PATCH` is admin-only, `created_by` comes from the session (body field forbidden, tested), and `job_created`/`job_updated` are audited with field names only. Unchanged PATCH writes no audit.
- **Migration:** integer IDs throughout, `entity_id` cast via `USING entity_id::integer`, the check constraint matches the model, and the downgrade reverses every step in order.
- **Error responses:** 401/403/404/409/422 use `{"detail": "<message>"}`, with no SQL or stack traces. An unauthenticated invalid body gets 401 before validation.
- **Ownership:** all changes are under `backend/`. No edits to contracts, `AGENTS.md`, scripts or `.env*` files.

## Verdict

**CHANGES REQUESTED.** There are no Critical or High findings. M-1 (role changes audited without old/new values) and M-2 (failed change-password not audited) fall short of audit requirements in `AGENTS.md` and `decisions.md` §14. Both are small, contained fixes. The Low findings can be fixed in follow-ups.
