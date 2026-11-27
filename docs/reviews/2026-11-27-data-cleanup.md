# Review: Phase 5.3 daily clean-up of working data

- **Branch reviewed:** `feat/backend-data-cleanup` (f3b8287, fixes in 531648a)
- **Reviewer:** independent review agent (Opus 5.5), read-only, running mutations on a copy of the code
- **Integration check:** Sonnet 5.5, with the real worker on seeded old and recent rows
- **Date:** 2026-11-27

## Scope

The maintenance job `cleanup_service.run_daily` runs once per business day, on one worker only (it takes an advisory lock and a `daily_runs` claim).

| Data | Removed when |
|---|---|
| Background tasks | Succeeded more than 30 days ago, or failed more than 90 days ago. A task an OCR result names as its source is kept. |
| Notifications | Read more than 90 days ago. Unread notifications stay, and so do the notifications of vehicle reminders that are still open. |
| Sign-in sessions | Expired or signed out more than 30 days ago. |
| Daily-run markers | More than 180 days old. |
| Leftover CAPTCHA images, state and answers | On any lookup that has finished. |

- **Never touched:** audit logs, customers, jobs, documents, OCR results, payments, receipts, vehicles, reminders and export records.
- **Batches:** rows are deleted 5,000 at a time, with a 60 s limit per run. A full batch lets the next loop carry on the same day.
- **Audit:** each run is audited with counts only.
- **Periods:** set with `RETENTION_*` settings, between 7 and 3650 days.
- **Overview:** the admin overview shows the job as "Data clean-up".
- **API:** no change.

## Findings

| ID | Severity | Finding | Outcome |
|---|---|---|---|
| M-1 | Medium | Deleting a read vehicle-expiry notification whose reminder was still open let the daily reminder check create it again, unread. That repeated every ~91 days for every expired reminder nobody had closed. | **Fixed.** Those notifications are kept. A regression test runs the clean-up, then the reminder check, and confirms there is still exactly one notification. |
| L-1 | Low | `DELETE ... WHERE id IN (subquery)`: a row changed while the delete waited for its lock was still deleted, for example a failed task an admin retried meanwhile. | **Fixed.** The condition is repeated on the outer DELETE, which PostgreSQL re-checks. |
| L-2 | Low | There was no index on `ocr_results.task_id`, so each deleted task meant a full scan of `ocr_results`. | **Fixed:** migration `f0a1b2c3d4e6`, plus a 60 s statement limit for the run. |
| L-3 | Low | Two rules were not in the approved list: deleting sessions after 30 days and daily-run markers after 180 days. | **For the owner.** Both are kept and safe: nothing refers to these rows, and logins and logouts stay in the audit log. |
| L-4 | Low | While a large first run continues, the overview briefly shows the clean-up as not run today. | **Accepted.** It clears itself within minutes. |
| L-5 | Low | Four of the reviewer's mutations survived. | **Fixed.** Tests added: a continuation removes only its own marker, every table can continue, failed and expired lookups are cleared, and the notification retention is checked at a non-boundary value. |

## Mutation checks

| Round | Result |
|---|---|
| Before the review | 13 mutations; the one that survived led to a new test: a retried task that keeps an old finish time. |
| The reviewer's 15 | 11 caught. All four survivors (M4, M6, M7, M13) are now caught. |
| After the fixes | The M-1 fix itself is caught when removed. |

L-1 is a race between two transactions and cannot be tested inside one. The reviewer reproduced it with two psql sessions.

## Integration check (real worker, seeded data)

The worker's first loop of the day ran the clean-up.

| Data | Removed | Kept |
|---|---|---|
| Background tasks | succeeded 40 days ago; failed 100 days ago | succeeded 10 days ago; failed 60 days ago; queued |
| Notifications | read 100 days ago; the done reminder's notification | read 30 days ago; unread 200 days ago; the open reminder's notification |
| Sessions | expired 40 days ago; signed out 40 days ago | expired 10 days ago; the current login |
| Vehicle lookups | CAPTCHA data cleared on the finished lookup | the waiting lookup's CAPTCHA |
| Daily-run markers | the one 200 days old | the one 100 days old |
| Audit logs | none | a row 4,000 days old |

- **Audit row:** one row with counts only and `finished: true`.
- **Overview:** shows "Data clean-up" with `ran_today: true`.
- **Restart:** the worker did not run it a second time that day.
- **Errors:** none from the clean-up.

## Checks run

- `bash scripts/check.sh backend`: **829 passed**, Ruff clean, OpenAPI contract in sync (no API change).
- Migration: down and up both run, and `alembic check` reports no drift.
