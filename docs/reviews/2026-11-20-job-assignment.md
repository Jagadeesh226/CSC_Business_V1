# Review: Phase 1 item 2, job assignment and assignment-scoped job access

- Branch: `feat/backend-job-assignment`, commit `bbe31d6`
- Diff: `git diff origin/main...feat/backend-job-assignment` (backend only, 9 files)
- Reviewer: review agent (findings only, no code changes)
- Date: 2026-11-20

## Verification performed

- `uv run pytest -q` against the test database: **174 passed**.
- `bash scripts/check.sh backend`: lock, Ruff lint, Ruff format, pytest **PASS**; contract check **FAIL (contract drift)**. This is expected (see Accepted gaps).
- Migration `c5b19e7d2a41` on the test database: downgrade, then upgrade round trip OK. `alembic check` reports no model/schema drift.
- Concurrency probes (real commits on the test database, tables truncated afterwards, 0 rows left):
  - Assign vs deactivate, with the assign transaction paused before commit while a second admin deactivated the same user. Both calls succeeded. The user ended up `is_active = false` with **1 active assignment** (see M-1).
  - Two admins unassigning the same user at the same time, both past the active-assignment read. Both returned 200, and **2** `job_unassigned` audit rows were written for one removal (see L-1).
  - The six-thread `test_concurrent_assignment_of_same_user` scenario, instrumented: all six workers passed the pre-check, so the result (1 assigned, 5 conflicts) comes from the partial unique index and the `IntegrityError` path. The test really races.
- HTTP probes (in a transaction that was rolled back):
  - For an employee, an unassigned job and a nonexistent job give identical responses (`404 {"detail": "Job not found."}`) on GET `/jobs/{id}`, PATCH `/jobs/{id}` and GET `/jobs/{id}/assignments`.
  - The `assigned_to` values `2147483648`, `9999999999` and `"me "` get 422 with the standard validation body. An employee's `assigned_to=<other user>` is intersected with their own visibility (total 0).
  - An employee assigned to a `completed` job can PATCH its title and due date (200, see L-2). An admin can unassign someone from a `completed` job (200, see L-3).

## Critical

None.

## High

None.

## Medium

### M-1: A user deactivated while being assigned keeps an active assignment

- **File:** `backend/app/services/job_service.py:229-251` (`assign_user`), `backend/app/repositories/job_repository.py:238-261` (`unassign_from_open_jobs`), `backend/app/services/user_service.py:173-191` (`deactivate_user`)
- **What is wrong:** `assign_user` reads `user.is_active` without locking the user row, then inserts the assignment. `deactivate_user` ends active assignments with one bulk `UPDATE ... WHERE unassigned_at IS NULL`. Under READ COMMITTED that UPDATE cannot see an assignment row that another transaction has inserted but not yet committed. Nothing serialises the two operations.
- **How to trigger:** Admin A sends `POST /jobs/{id}/assignments {user_id: X}` while admin B sends `POST /users/X/deactivate`, so that B's UPDATE runs between A's `is_active` check and A's commit (or B commits after A's check). The probe paused A before commit and got: both succeed, user X inactive, 1 active assignment on an open job, and no `job_unassigned` row.
- **Impact:** This breaks the owner rule that deactivation removes a user from open jobs. The job lists an inactive person as an assignee. If X is reactivated later, X silently gets access to that job again, although every other open-job assignment was removed. It needs two admins acting at the same moment, so it is unlikely but possible.
- **Suggested fix:** Serialise on the user row. In `assign_user`, load the assignee with `SELECT ... FOR UPDATE` (for example a `user_repository.get_user_for_update`) before checking `is_active`. `deactivate_user` already updates that row, so the two operations will queue: either the assign sees `is_active = false` (422), or the deactivation's bulk UPDATE runs after the assignment is committed and removes it. Add a two-session test like the probe.

## Low

### L-1: Concurrent unassign of the same assignment writes two audit entries and overwrites `unassigned_by`

- **File:** `backend/app/services/job_service.py:254-272`
- **What is wrong:** `unassign_user` reads the active row, then updates it through the ORM by primary key only. A second concurrent request that read the same row waits for the first commit, then updates the row again. It overwrites `unassigned_at` / `unassigned_by` and records a second `job_unassigned`.
- **How to trigger:** Two admins send `DELETE /jobs/{id}/assignments/{user_id}` at the same time (probe: both 200, 2 audit rows, final `unassigned_by` is whichever admin committed last). The same overwrite can happen if a manual unassign races with deactivation.
- **Impact:** The audit log shows two removals of one assignment by different admins, and the history row may name the wrong remover. Audit accuracy only; access control is not affected.
- **Suggested fix:** Use a conditional UPDATE, `UPDATE job_assignments SET ... WHERE id = :id AND unassigned_at IS NULL RETURNING id`, and return 404 ("not assigned") when no row comes back. Alternatively, load the row with `FOR UPDATE` and re-check `unassigned_at`.

### L-2: Assigned employees can edit completed and cancelled jobs

- **File:** `backend/app/services/job_service.py:177-204`
- **What is wrong:** `update_job` has no final-status check. Before this change only admins could PATCH jobs. Now any employee who keeps an assignment on a final job can change it, and deactivation intentionally keeps assignments on final jobs "as a record".
- **How to trigger:** Assign an employee, set the job to `completed`, then as the employee `PATCH /jobs/{id} {"title": "...", "due_date": "2020-01-01"}`. The probe got 200.
- **Impact:** `decisions.md` §7 says completed and cancelled jobs are final. An employee can rewrite the title, description or due date of finished work. The change is audited (`job_updated`, field names only).
- **Suggested fix:** Ask the owner whether final jobs are read-only. If so, reject PATCH on `completed`/`cancelled` jobs with 422 for employees (or for everyone), and add a test. This can land with the status-transition step if the owner prefers.

### L-3: Manual unassign is allowed on final jobs, but deactivation preserves those rows

- **File:** `backend/app/services/job_service.py:254-260`
- **What is wrong:** `assign_user` refuses final jobs, and deactivation deliberately keeps final-job assignments as the record of who did the work. `unassign_user` has no status check, so an admin can still remove the assignee from a completed job.
- **How to trigger:** `DELETE /jobs/{completed_job}/assignments/{user_id}` as admin returns 200 (probe).
- **Impact:** The current-assignee "record" on final jobs can be erased (history is kept in the row and audit log). The rules for final jobs are inconsistent.
- **Suggested fix:** Ask the owner. If final jobs are frozen, return 422 from `unassign_user` for `FINAL_STATUSES`, the same way `assign_user` does.

### L-4: `assignees` is not marked required in the `JobRead` response schema

- **File:** `backend/app/schemas/job.py:101`
- **What is wrong:** Because of `assignees: list[AssigneeRef] = []`, the generated OpenAPI lists `JobRead.required` without `assignees`. The server always returns it.
- **How to trigger:** `app.openapi()["components"]["schemas"]["JobRead"]["required"]` does not contain `assignees`.
- **Impact:** Generated frontend types will declare `assignees?:`, which forces needless undefined checks. Fix this before the owner commits the contract regeneration.
- **Suggested fix:** Declare it as `assignees: list[AssigneeRef]` with no default (`_to_read` always sets it), or use `Field(default_factory=list)` together with `json_schema_extra`/a serialization-required setting.

## Accepted gaps

- `docs/contracts/openapi.json` is not regenerated (owner PR), so `check.sh` reports contract drift. The new routes, the `assigned_to` query parameter and `JobRead.assignees` need owner approval and regeneration.
- Assign vs job status change: `assign_user` reads `job.status` without a lock. No endpoint changes status yet (later step), so this cannot be triggered today. When status changes and cancel land, lock the job row (`FOR UPDATE`) in both `assign_user` and the status/cancel path. Deactivation's open-job subquery has the same consideration.
- Assigned employees see the full assignment history of their job, including names of people who were removed and of the admins who assigned them. This matches "View job history: Assigned" in `permissions.md`; the response contains names and IDs only.
- No rate limiting (Phase 6).

## Checked and correct

- **Admin-only enforcement:** POST and DELETE on assignments use `AdminUser`. An employee gets 403 and anonymous gets 401 before any existence check, so 403 reveals nothing about the job.
- **Record-level access:** `_ensure_job_access` is applied on GET `/jobs/{id}`, PATCH `/jobs/{id}` and GET `/jobs/{id}/assignments`. List visibility is enforced in SQL (`EXISTS` on an active assignment) for both `items` and `total`, so search, filters, pagination totals and `assigned_to` cannot reveal unassigned jobs. Body validation (422) runs before the lookup but does not depend on the job, so it reveals nothing.
- **404 equivalence:** Unassigned and nonexistent jobs return the same status and body for employees.
- **Unassignment and demotion:** Access is checked per request against `unassigned_at IS NULL`, so it ends immediately (tested). A demoted admin is limited to assigned jobs because the role is read on every request.
- **Auto-assign on creation:** Applies only to non-admin creators. It runs in the same transaction as the job insert, so a job-number collision retry rolls it back and re-adds it once. It is audited as `job_assigned` with `reason: "creator"`.
- **Duplicate assignment:** The pre-check gives 409, and the partial unique index `uq_job_assignments_active` gives 409 under a race (verified to exercise the `IntegrityError` path). Re-assigning after removal creates a new history row.
- **Deactivation:** The bulk UPDATE is limited to the user's active rows on non-final jobs, in the same transaction as `user_deactivated`. One `job_unassigned` audit row is written per affected job with `reason: "user_deactivated"` and the acting admin. Other users' rows and final-job rows are untouched. Already-inactive users skip the block.
- **Audit:** `job_assigned` and `job_unassigned` record the job as entity (integer ID), the acting user, and the target `user_id`. `job_updated` now records the employee as actor.
- **Queries:** `active_assignees` loads a page's assignees in one query. The N+1 test counts the statements that touch `job_assignments` during an admin list request, and the count is meaningful because the admin list query itself does not reference that table. `get_job` uses a fixed three queries.
- **`assigned_to` parsing:** The pattern rejects `0`, negatives, leading zeros, decimals, whitespace and values over 10 digits. Values over `MAX_DB_INT` get 422 in the standard validation format. `me` resolves to the signed-in user.
- **Migration:** `c5b19e7d2a41` adds a nullable `unassigned_by` with a named FK. It is reversible, does not edit an earlier migration, and has no model drift.
- **AGENTS.md:** Changes are confined to `backend/`. No dependencies were added, no UUIDs, no tests weakened (the replaced employee-access tests reflect the owner-approved rule change). Existing routes, fields and status codes are unchanged, except that PATCH `/jobs/{id}` now returns 404 instead of 403 to employees for unassigned jobs, as the owner decided.

## Verdict

**APPROVED.** No Critical or High findings. M-1 should be fixed before or soon after merge (small change: lock the assignee row in `assign_user`). L-2 and L-3 need an owner decision on whether final jobs are frozen.
