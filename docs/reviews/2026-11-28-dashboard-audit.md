# Review: Phase 6.0 dashboard summary and audit-log review

- **Branch reviewed:** `feat/backend-dashboard-audit` (063f9ef; fixes in afcc42a and a test commit)
- **Reviewer:** independent review agent (Opus 5.5), read-only, running mutations on a copy of the code
- **Integration check:** Sonnet 5.5, over real HTTP
- **Date:** 2026-11-28

## Scope

These are the backend pieces the upcoming screens need.

- **`GET /api/v1/dashboard`** (any signed-in user): job counts on the centre's calendar.
  - Counts: created today and this month, open jobs by status, overdue, due today, completed today and this month, cancelled this month.
  - Recent jobs, ordered by latest activity.
  - Employees see their assigned jobs only.
  - Admins see every job, plus each active person's workload and the number of open jobs with nobody assigned.
- **`GET /api/v1/audit-logs`** (admins only): newest first.
  - Filters: action, record, person and date range (at most one year).
  - Each entry shows the person and its details as an object.
- **Migration `a1b2c3d4e5f7`:** indexes on the audit log and on status-change times.
- **Left to existing endpoints:**
  - The other dashboard tiles: the work queue, expiry buckets and collections.
  - A customer's payments: receipts already filter by customer.

## Findings

| ID | Severity | Finding | Outcome |
|---|---|---|---|
| F1 | Medium | No test protected the employee scope on completed and cancelled counts. Removing it (mutation M1) left every test passing. | **Fixed.** A test checks that an employee sees only her own completions and cancellations. |
| F2 | Low | Three gaps survived mutation: the unassigned count included closed jobs; the workload's overdue count included closed jobs; a completion was credited to someone who had been taken off the job. | **Fixed,** with one test for each. |
| F3 | Low | "Recent jobs" move only when a job is created or changes status. Edits, uploads and payments do not move a job up. | **For the owner.** Changing it would need `jobs.updated_at`. |
| F4 | Low | Workload rows include admins, and a job completed by two people counts for both. | **Documented** in the API schema. Owner to confirm. |
| F5 | Low | No indexes for the audit-log page, and the month counts could not use an index. | **Fixed.** Three audit-log indexes and one on `job_status_history.changed_at`. The month counts now compare timestamps. |
| F6 | Low | The one-year limit was off by one. | **Fixed.** It counts days inclusively, with a boundary test. Open-ended ranges are allowed, since results are paged. |
| F7 | Low | Reading the audit log is not itself audited. | **For the owner.** AGENTS.md does not require it. |
| F8 | Info | The contract check fails on the branch until the contract PR merges. | Contract PR opened. |

## Mutation checks

| Round | Result |
|---|---|
| Before the review | 13 mutations; the 3 that survived (the India-time boundary, overdue counting closed jobs, ended assignments) got new tests. |
| The reviewer's 18 | 14 killed. All 5 survivors (M1, M2, M6, M12, M17) are now killed. |

## Integration check (real HTTP)

All 6 checks passed.

| Check | Result |
|---|---|
| Employee A's dashboard | Only her 2 jobs: 1 overdue, 1 in progress. No workload section. |
| Employee B's dashboard | Her cancelled job. Nothing of A's. |
| Admin's dashboard | All 4 jobs, everyone's workload, 1 unassigned job, newest activity first. |
| Signed out | Dashboard returns 401. |
| Audit log as an employee | 403. |
| Audit log as the admin | Entries newest first. Filters by record, person and date work. A 367-day range and page size 101 return 422. |
| Secrets in audit details | No passwords, tokens or CSRF values in any entry. |

The check reported that the cancel entry had no details. A test through the API shows the entry does record "from", "to" and the reason, so that observation was mistaken. The test is kept.

## Checks run

- `bash scripts/check.sh backend`: **848 passed**, Ruff clean. The contract check passes once the contract PR merges.
- The contract diff is additions only: 2 paths and 8 schemas.
- Migration: down and up both run, and `alembic check` reports no drift.
