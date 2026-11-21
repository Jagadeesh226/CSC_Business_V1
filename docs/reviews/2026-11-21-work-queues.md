# Review: Phase 2.4 work queues and job readiness

- **Branch reviewed:** `feat/backend-work-queues` (3569ec4, fixes in b22c556)
- **Reviewer:** independent review agent (Opus 5.5), read-only
- **Date:** 2026-11-21

## Scope

- `document_progress` on every job
- `GET /jobs` filters: `readiness`, `due`, `unassigned`, `sort=urgency`
- `GET /work-queue`
- `GET /review-queue`
- tests in `backend/tests/test_queues.py`

## Findings

| ID | Severity | Finding | Outcome |
|---|---|---|---|
| M1 | Medium | `GET /review-queue?job_id=` let an employee find out whether a job they are not assigned to exists, and which documents it shares, through `total`. | **Fixed.** A hidden job now returns 404, the same as a job that doesn't exist. Covered by a test, which fails if the fix is removed. |
| M2 | Medium | A job with no required documents counts as `ready_to_submit` / `ready_to_resume`. | **Kept, by design.** This matches the submit check, which doesn't block such a job. Documented and tested. |
| M3 | Medium | The contract has not been regenerated yet. | **Additions only:** 2 routes, 9 schemas, optional `JobRead.document_progress`, optional `/jobs` query parameters. To be regenerated on the owner contract branch. |
| L1 | Low | Review-queue items show the filename and customer phone, as other lists already do. Filenames may contain ID numbers. | **Deferred.** This affects every list, not just this one; suggested for Phase 6 hardening. |
| L2 | Low | The review-queue count and page used different joins. | **Fixed.** Both now share the same base query. |
| L3 | Low | Readiness filters added up progress for every job ever created. | **Fixed.** Only jobs still gathering documents are added up. |
| L4 | Low | Test gaps. | **Added:** the job-filter probe, mixed documents on one item, and readiness for a job that needs no documents. A test that freezes the clock near midnight India time is not added. |

## Checked and found sound

- **Progress matches the checklist.** The SQL progress matches `build_checklist` for:
  - archived documents
  - which state wins
  - several documents on one item
  - links to another job's checklist item
- **Subqueries** are correlated correctly.
- **Employee scoping:**
  - every work-queue count is limited to the employee's assigned open jobs
  - `unassigned` returns an empty list for employees
  - the jobs shown on a review item are limited to the employee's assigned open jobs
- **Performance:**
  - single-job reads use the existing indexes
  - no N+1 queries
- **Dates and order:**
  - the business date comes from the database, in Asia/Kolkata time
  - urgency order is deterministic
- **No status changes:** nothing changes a job's status automatically.
- **Other rules:** integer IDs only, and no new dependencies.

## Checks

- **Full suite:** `scripts/check.sh backend` gives 482 passed. The only failing check is the expected contract difference.
- **Deliberate breaks:** the tests caught 10 out of 10 changes that broke the scoping, state and filter rules on purpose.

## Integration check

The integration agent (Sonnet 5.5) ran the real API and the real worker over HTTP, using the fake OCR provider, the test database and Redis database 15. All 8 steps passed:

1. Catalogue setup.
2. Each employee's job was created and assigned to them.
3. Progress, work-queue counts and the readiness filter were scoped to each person's own jobs.
4. OCR moved the Aadhaar to `ocr_check` with 2 flagged fields, and the photo showed as `manual`.
5. An employee could not see another employee's queue, and `job_id` for a hidden job returned 404.
6. After review and verification the job became `ready_to_submit` and was submitted.
7. The `due=overdue` filter and urgency order worked.
8. The contract difference was additions only.
