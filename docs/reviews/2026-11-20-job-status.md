# Review: job status changes and status history (Phase 1 item 3)

- **Branch:** `feat/backend-job-status` at `2205804`
- **Diff:** `git diff origin/main...feat/backend-job-status` (backend only: routes, schema, service, repository, model, migration `d8e3f4a6b7c9`, tests)
- **Reviewed against:** `AGENTS.md`, `backend/AGENTS.md`, `docs/contracts/decisions.md` §7 and §14, `docs/contracts/permissions.md`, previous reviews, and the owner decisions listed in the review request.
- **Reviewer:** review agent. Findings only; no application code was changed.

## Checks run

- `TEST_DATABASE_URL=... uv run pytest -q` (test database `csc_crm_test` only): **218 passed**.
- `bash scripts/check.sh backend`: dependency lock, Ruff lint, Ruff format, pytest **PASS**; contract check **FAIL (contract drift)**. Expected, because the owner has not yet updated `docs/contracts/openapi.json` (see Accepted gaps).
- Probes (scratch scripts, test database only, all rows truncated afterwards, every session closed in `finally`). The probes use `autoflush=False`, the same setting as the app's `SessionLocal`:
  1. Real `user_service.deactivate_user` paused in `before_commit` while `assign_user` ran in another session. Both succeeded. Result: the user was `is_active = false` with **1 active assignment** (see M-1).
  2. A status change to `completed` flushed but not committed (holding the job row) while `deactivate_user` ran for the assignee. The deactivation **did not wait**. Result: job `completed`, **0 active assignees**, one `job_unassigned` audit with `reason: user_deactivated` (see M-2).
  3. Session A started its transaction (as the auth query does), then session B moved the job `pending -> in_progress` and committed, then A moved it `in_progress -> submitted`. `GET status-history` returned ids `1, 3, 2`: the `in_progress -> submitted` row is listed **before** `pending -> in_progress` (see L-1).
  4. Migration: downgrade to `c5b19e7d2a41`, insert jobs in `pending`, `in_progress`, `completed` without history, upgrade. Each job got `None -> pending` (changed_by = creator, changed_at = created_at), and the two non-pending jobs got `pending -> <status>` with the marker reason. Downgrade kept the 5 rows. Re-upgrade with a job in `on_hold` failed on `ck_jobs_status` and left the revision at `c5b19e7d2a41` with no new rows. After removing that job, re-upgrade added no duplicates (still 5 rows). The test database was left at head `d8e3f4a6b7c9`.

## Critical

None.

## High

None.

## Medium

### M-1: Deactivation still misses a concurrent assignment (fix for the previous M-1 does not work in the app's session setup). Pre-existing, outside this diff

- **File:** `backend/app/services/user_service.py:179-190` (`deactivate_user`), `backend/app/db/session.py:15` (`sessionmaker(autoflush=False)`), `backend/app/services/job_service.py:245-269` (`assign_user`), `backend/tests/test_job_assignments.py:421-437`
- **What is wrong:** The fix for the job-assignment review's M-1 locks the user row in `assign_user` and relies on `deactivate_user` updating that row *before* its bulk unassign. The app session has `autoflush=False`, so `user.is_active = False` is only written at `db.commit()`. The real order of statements in `deactivate_user` is: `UPDATE user_sessions`, then the bulk `UPDATE job_assignments`, then `UPDATE users` (at commit). The bulk unassign runs before the user row is locked, so `assign_user` can lock the user, see `is_active = true`, insert and commit in that window. The regression test does not call `deactivate_user`. It sets `is_active` and calls `flush()` by hand, which is the order the fix assumed, not the order the app uses.
- **How to trigger:** Admin B deactivates employee X while admin A assigns X to an open job, so that A's assign runs after B's bulk unassign and before B's commit. Probe 1 reproduces this with the real functions: both calls succeed, X is inactive, X keeps 1 active assignment, and no `job_unassigned` audit is written for it.
- **Impact:** Same as the earlier M-1: an inactive user is listed on an open job, and gets that job back silently if reactivated. It needs two admins acting at the same moment.
- **Suggested fix:** Take the user lock at the start of `deactivate_user`, before the bulk unassign. Do it with `user_repository.get_user_for_update` (or an explicit `db.flush()` right after `user.is_active = False`), and keep the lock order consistent with M-2 (see that fix). Change the regression test so it runs the real `deactivate_user` in an `autoflush=False` session and pauses it before commit, as probe 1 does.

### M-2: Deactivation can remove the assignee from a job that is being completed or cancelled

- **File:** `backend/app/repositories/job_repository.py:281-302` (`unassign_from_open_jobs`, subquery at line 292), `backend/app/services/job_service.py:315-327`, `backend/app/services/job_service.py:363-401` (`change_status`, `cancel_job`)
- **What is wrong:** This branch makes every status-dependent write lock the job row, but deactivation's bulk unassign does not. It chooses "open" jobs with a plain subquery (`jobs.status NOT IN (final)`). Under READ COMMITTED that subquery sees the last committed status and does not wait for a transaction that is closing the job. Updating `job_assignments` rows does not conflict with the lock on the `jobs` row.
- **How to trigger:** An admin or the assigned employee moves job J `submitted -> completed` (or an admin cancels J) while another admin deactivates the assignee. Probe 2: the deactivation did not block on the closer's job-row lock. After both commits J is `completed` with 0 active assignees and a `job_unassigned` audit (`reason: user_deactivated`).
- **Impact:** This breaks the owner rule that assignments are frozen on closed jobs ("Closed jobs keep their assignments as the record of who did the work"). The completed job shows no assignee, and the audit trail shows an unassignment from a closed job. The ended assignment row is kept, so `GET /assignments` still shows who held it.
- **Suggested fix:** In `unassign_from_open_jobs`, first lock the affected jobs: `SELECT jobs.id ... JOIN job_assignments (user, active) ... ORDER BY jobs.id FOR UPDATE OF jobs`. Then re-check their status and end only the assignments on jobs that are still open. **Lock order:** `assign_user` locks job then user. If deactivation locks the user first (M-1 fix) and then jobs, the two can deadlock. Example: X is already assigned to J, and A's duplicate assign holds J and waits on X, while B holds X and waits on J. Postgres aborts one with a deadlock error, which becomes a 500. Use one order everywhere. Either lock the user's open jobs (ascending id) *before* the user row in `deactivate_user`, or lock the user first in `assign_user` too. Add a two-session test: a status change to `completed` held uncommitted, and deactivation must wait and then leave the assignment in place.

## Low

### L-1: Status history is ordered by transaction start time, so concurrent changes can be listed out of order

- **File:** `backend/app/repositories/job_repository.py:323` (`order_by(JobStatusHistory.changed_at, JobStatusHistory.id)`), `backend/app/models/job_status_history.py:26-28` (`server_default=func.now()`)
- **What is wrong:** `changed_at` is `now()`, which is the start time of the transaction, not the time the change was made. The request's transaction starts at the auth query, before the job row lock is taken. If request A starts first but request B gets the lock first, B's row has the later `changed_at` and the smaller `id`. Listing by `changed_at` then puts A's change (which really came second) first. Within one transaction there is never more than one history row today, so the `id` tie-break alone is not the problem.
- **How to trigger:** Two requests on the same job within milliseconds, both valid in sequence (for example `-> in_progress` then `-> submitted`). Probe 3 returned history ids `1, 3, 2`: `in_progress -> submitted` listed before `pending -> in_progress`.
- **Impact:** The history reads as an impossible sequence, and `changed_at` decreases between consecutive entries. Data is not lost. A request that waits a long time on the lock also gets a timestamp earlier than the real change.
- **Suggested fix:** Order by `id` only (ids are allocated after the lock is held, so `id` order matches lock order), or set `changed_at` explicitly with `clock_timestamp()` / `func.clock_timestamp()` at insert time. Ordering by `id` is the smaller change. The backfilled rows stay correct either way (creation row first, marker row second).

### L-2: The new locks in `update_job` and `unassign_user` have no race tests

- **File:** `backend/app/services/job_service.py:193` (`update_job`), `backend/app/services/job_service.py:277` (`unassign_user`), `backend/tests/test_job_status.py:377-427`
- **What is wrong:** The two race tests cover status vs status and assign vs close. Nothing fails if `get_job_for_update` in `update_job` or `unassign_user` goes back to `get_job`. An employee PATCH racing a close, or an admin unassign racing a close, would then pass the open-job check on stale data.
- **How to trigger:** Swap `get_job_for_update` for `get_job` in either function and run the suite. It stays green. (Checked by reading the code; mutation not run.)
- **Impact:** Test gap only. The current code is correct.
- **Suggested fix:** Add two `_run_while_locked` tests: an employee `update_job` on a job held mid-`completed` must raise `BusinessRuleError`, and `unassign_user` on a job held mid-`cancelled` must raise `BusinessRuleError` and leave the assignment active.

## Accepted gaps (owner decisions, not findings)

- `docs/contracts/openapi.json` not yet updated for the three new routes and the required `allowed_next_statuses` field, so `check.sh` reports contract drift. Owner PR pending. Note for that PR: adding a **required** response field is additive for clients but must be in the contract before the frontend relies on it.
- Admins follow the same moves as employees (no skipping). Reason optional except for cancel.
- The creation entry is the first history row, and existing jobs are backfilled. Backfilled `pending -> <current>` rows are not an allowed move and carry the marker reason, `changed_by = NULL` and `changed_at` = migration time.
- Employees may complete their assigned jobs. Cancel is admin-only and needs a reason. Completed and cancelled jobs are final.
- Closed jobs are read-only for employees, admins may still edit them, and assignments are frozen on closed jobs.
- The migration fails if there are unknown statuses instead of rewriting them. Downgrade keeps history rows.
- No rate limiting (Phase 6). Fees are the next step.

## Checked and correct

- **Transition rules:** `STATUS_TRANSITIONS` matches decisions.md §7 exactly. `test_allowed_moves_match_rules` covers every (current, target) pair among the five non-cancel targets for all six statuses, both the 200 and 422 results, and `allowed_next_statuses`. Same-status moves are rejected (a status is never in its own list, and the DB check `old_status <> new_status` is a second guard). `cancelled` through `/status` gives 422 with a pointer to cancel. Cancel works from all four open statuses and is refused from `completed`/`cancelled`.
- **Authorization:** `/status` uses `CurrentUser` and `_ensure_job_access` *after* taking the lock: admins any job, employees only active assignments, otherwise 404, the same as for a missing job (no existence leak). `/cancel` uses `AdminUser`, so an employee gets 403 whether or not the job exists, the same as the other admin-only job routes (no leak). `/status-history` applies the same assignment check and returns 404 to unassigned or formerly assigned employees. `changed_by` exposes only `{id, name}`. All three routes need sign-in (401) and the POSTs need CSRF (403). Tested.
- **Unassign vs status change:** both lock the job row, so an employee's status change after a committed unassign sees the unassign and gets 404.
- **Lock behaviour of the new code:** status change, cancel, PATCH, assign and unassign take `FOR UPDATE` on the job first. Status changes write `changed_by`/`user_id` FKs, which take `FOR KEY SHARE` on user rows. That does not conflict with deactivation's `UPDATE users` (`FOR NO KEY UPDATE`), so there is no job/user deadlock from this branch. The only lock-order risk is the one described in the fix for M-2.
- **`populate_existing`:** needed and used correctly. Without it, a `Job` already in the identity map would keep its stale `status` after the lock. No caller has unflushed changes on the job before the lock, so nothing is overwritten.
- **Transactions:** one commit per operation. Errors raised before commit roll back through `get_db`. History, audit and status change go into the same transaction. Creation's history row is inside the existing collision-retry loop, so it is rolled back and recreated with the job.
- **Audit:** `job_status_changed` and `job_cancelled` with `from`, `to`, and `reason` when present, and the acting user. Tested.
- **Validation:** `extra="forbid"` on both bodies. Reason is stripped, 1–1000 chars, and an empty or whitespace-only reason is 422 (not silently treated as none). Unknown status is 422. Unknown job is 404. Job id path validation is reused.
- **Queries:** history is one query with an outer join to users (no N+1). It is unbounded per job, which is acceptable for a per-job lifecycle log. `allowed_next_statuses` is computed in memory, with no extra list queries.
- **Migration:** see probe 4. Upgrade fails atomically on bad data. Backfill is idempotent across downgrade and upgrade. The model's `ck_jobs_status` matches the migration.
- **AGENTS.md:** changes only under `backend/`. No contract edits, no new dependencies, integer ids throughout, no tests weakened (`test_jobs_api.py` only adds the new key to the expected set). No scope creep: no fee or other Phase-1 work.

## Verdict

**CHANGES REQUESTED.** There are no Critical or High findings, but M-2 is introduced by this change's own locking design: it breaks the owner rule that assignments are frozen on closed jobs. It should be fixed together with M-1 (pre-existing, but the same code path, and the two fixes must agree on lock order). L-1 is a one-line fix and worth doing in the same pass. L-2 is a test gap.
