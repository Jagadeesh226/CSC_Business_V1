# Review: Phase 5.2 background Excel exports

- **Branch reviewed:** `feat/backend-background-exports` (a1a9321, fixes in e1a2d42)
- **Reviewer:** independent review agent (Opus 5.5), read-only, running mutations on a copy of the code
- **Integration check:** Sonnet 5.5, over real HTTP with the real worker
- **Date:** 2026-11-27

## Scope

- **Requesting an export:** `POST /api/v1/exports` takes the kind (`vehicles` or `jobs`) and its filters.
  - Unknown fields and free-text search are refused.
  - Limits per person: three exports waiting at once, and twenty a day.
- **Building it:** the `report_export` task builds the file in the background with what the requester may see at that moment.
  - It holds at most 50,000 rows and must finish within 10 minutes. It is not retried.
  - The file goes into the private, encrypted storage.
  - The requester is notified when it is ready (`export_ready`) or has failed (`export_failed`).
- **Downloading:** `GET /exports/{id}/file`.
  - Only the requester can download it, for 24 hours. Anyone else gets 404, admins included.
  - The file is checked against its SHA-256 and the storage's own authentication.
  - It is refused if the requester's role has changed since the export was built.
  - Queued or failed exports return 409; expired ones return 410.
- **Expiry:** after 24 hours, maintenance deletes the file and keeps the record.
- **New jobs export:** uses the jobs list's filters. Mobile numbers are masked and formula text is escaped.
- **Existing download:** the direct vehicle export now shares its builder and behaves as before.
- **Audit:** requests, builds, failures and downloads are recorded. Filters only; no personal data.

## Findings

| ID | Severity | Finding | Outcome |
|---|---|---|---|
| F1 | **High** | A job with no service, such as one created before the catalogue existed, crashed every jobs export, including the admin's. | **Fixed,** with a test. |
| F2 | Medium | A file saved by a run whose record was never committed stayed on disk indefinitely: timed out, taken over, rolled back, or a failed commit. | **Fixed.** A saved file is deleted unless the transaction that records it commits. A stale run deletes its file at once. |
| F3 | Medium | A 100k-row jobs export could not finish in 300 s: OFFSET paging, a count on every page, and the session never cleared. Its timeout was retried. | **Fixed.** The matching ids are taken once, rows are read 500 at a time and the session is cleared after each chunk. The limit is 50,000 rows and the time 10 minutes, with no retry. Measured: 5,000 jobs in 0.6 s. |
| F4 | Medium | Rows could repeat or go missing while jobs changed during the build. | **Fixed** by the single id snapshot. |
| F5 | Medium | Vehicle exports with more than 65,535 rows broke PostgreSQL's limit on bind parameters. | **Fixed.** Validity dates and people are fetched in chunks. |
| F6 | Medium | Access was not re-checked at download. An admin demoted to employee could still download an all-jobs file. | **Fixed for role changes:** the role used for the build is stored, and a download under a different role gets 410. **Owner to note:** being taken off a job does not withdraw an export already made. It expires within a day. |
| F7 | Low | A tampered file returned a 500, not a 410. | **Fixed.** |
| F8 | Low | Expiry forgot about a file it could not delete. | **Fixed.** The record and key are kept, and deletion is retried on the next maintenance run. |
| F9 | Low | A temporary error such as "trying again" was shown as the final reason. | **Fixed.** Only our own fixed sentences are shown; anything else gets a plain generic reason. |
| F10 | Low | The number of exports a person could make was unbounded. | **Fixed.** Twenty a day per person. |
| F11 | Low | Contract changes are additions only, but `TaskType` gains `report_export`, and frontend code that handles every task type needs to know the new value. | **For the owner** in the contract PR. |

## Mutation checks

- **Before the fixes:** the reviewer's mutations killed 11 of 18. All seven survivors (M6, M7, M10, M14–M17) were covered by new tests, except M6: removing the user-row lock that serialises concurrent requests against the limit. A race like that is not testable in one transaction, and the lock is kept.
- **After the fixes:** 14 mutations targeting the fixes. All 14 were caught. They covered:
  - a job without a service
  - reading only the first chunk of jobs or vehicles
  - stray files kept after a rollback, a stale run, or an unsaved run
  - recovery failing a task that is still waiting
  - the failure hook overwriting a finished export
  - failed exports counted as waiting
  - no daily cap
  - tampering not caught
  - no role check
  - raw failure reasons shown
  - forgetting a file that could not be deleted
- **Earlier round:** a further 20 mutations on access, masking, escaping, limits, audit, expiry and notices were caught, except one equivalent: an early stale check that the check under the lock covers.

## Integration check (real HTTP, real worker)

All 10 checks passed:

1. **Employee's jobs export:**
   - Downloading too early gets 409.
   - Once ready, she is notified and the file holds exactly her 2 jobs.
   - A formula title is stored as `'=HYPERLINK(...)`, the mobile number is masked, and the full number appears in no cell.
   - Downloading marks the notification read.
2. **Isolation:** another employee and an admin both get 404.
3. **Admin export:** holds all 3 jobs.
4. **Validation:** search and unknown kinds get 422.
5. **Vehicles export:** the chassis number is masked and there is no WhatsApp column.
6. **Limits:** a fourth waiting export gets 409.
7. **Storage:** stored files are encrypted, with no names in plain text.
8. **Expiry:** the download gets 410, and maintenance deletes the file.
9. **Role change:** the download gets 410, and works again after the role is restored.
10. **Audit:** requests, builds and downloads are all recorded, with no personal data.

## Checks run

- `bash scripts/check.sh backend`: **811 passed**, Ruff clean. The contract check passes once the contract PR merges.
- Migration: down and up both run, and `alembic check` reports no drift.
