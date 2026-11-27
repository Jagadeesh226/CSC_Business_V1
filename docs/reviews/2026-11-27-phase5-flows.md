# Review: Phase 5.4 whole-flow tests for the back office

- **Branch reviewed:** `feat/backend-phase5-flows` (c7cd3ce, fixes in 171f846 and af2a8b0)
- **Reviewer:** independent review agent (Opus 5.5), read-only, running mutations on product code
- **Integration check:** not run. This step changes tests only, and these tests already drive the real worker.
- **Date:** 2026-11-27

## Scope

`backend/tests/test_phase5_flows.py` tells three stories through the API with the real worker. The worker is registered with the production maintenance set, and that set is checked.

1. **A day of work**
   - An admin assigns a job, and only the assignee is told.
   - An unreadable scan is sent to her alone, and the admins are not alarmed.
   - The rejection settles it.
   - She requests a jobs export. It holds only her job, the mobile number is masked, and only she can download it.
   - Downloading settles the notice.
   - A second unreadable copy arrives, then an admin submits with an audited override, and she completes the job. Completing it settles everything still open.
   - The audit trail holds every step and no customer details.
2. **An outage**
   - The worker reports in through its own heartbeat code, and the day's jobs run on its first loop.
   - Retries used up reach each admin once, as a count. Employees get nothing. The overview shows a warning, not a problem.
   - An OCR re-run waits in the queue.
   - A worker that goes quiet appears as a plain problem. A clean shutdown shows it as stopped.
   - Readiness says only "ok".
3. **Time passing**
   - The export file is deleted after a day, and the record is kept.
   - The daily clean-up removes the old finished task and the read notice. It keeps the task an OCR result names, a recent task, a failed task under 90 days and an old unread notice. Counts are exact, and a later loop does not run it again.

One other test changed: `tests/test_reminders.py::test_daily_check_runs_in_the_real_worker` relied on a module's first import registering a job. It failed once these flows imported that module first. It now reloads the module, as the other worker tests do.

## Findings

| ID | Severity | Finding | Outcome |
|---|---|---|---|
| F1 | Medium | Other people's inboxes were checked only once. An `export_ready` also sent to admins was caught by nothing in the whole suite. | **Fixed.** Nobody else's inbox is checked after every step. |
| F2 | Medium | The job was set to `submitted` with SQL, bypassing the document check and the audit, and closed with nothing left unread. That meant the Phase 5.1 close-settling bug (autoflush) could not be caught. | **Fixed.** The job is submitted through the API with an admin override, and closed while a failed-reading notice is unread. |
| F3 | Medium | "Only working data goes": no old row that must survive was present. | **Fixed.** Four rows that must survive were added, with exact counts. |
| F4 | Low | "Does not run a third time" was guaranteed by the unique key, not by the code. | **Fixed.** A later loop adds no clean-up audit row. |
| F5 | Low | Counts were checked with `>= 1`. | **Fixed.** Exact counts. |
| F6 | Low | Heartbeats were inserted by hand, so a broken `beat` or `prune` went unnoticed. | **Fixed.** The worker uses `register`, `beat` and `mark_stopped`, and goes quiet first, so only a real beat shows it running. |
| F7 | Low | The fixture registered a hand-picked set of jobs, so a production module dropping the clean-up went unnoticed. | **Fixed.** `reminder_jobs` and `ops_jobs` are reloaded, and the production set is asserted. |
| F8 | Low | The re-run OCR task was never shown to be queued. | **Fixed.** |
| F9 | Low | The audit check looked only for the phone number. | **Fixed.** Customer names and the second number are checked too. |

## Mutation checks

| Mutations | Result |
|---|---|
| The reviewer's 34, before the fixes | The flows killed 18 (2 only by accident). Of the 16 they missed, the unit suites caught 15, and one (`export_ready` to admins) was caught by nothing. |
| After the fixes: the 13 survivors that matter, plus a "stop not recorded" mutation | All killed by the flows: export_ready to admins, ocr_failed to everyone, no flush on close, close not settling OCR notices, tasks deleted at any age, the OCR source not kept, failed tasks after 30 days, unread notices deleted, the daily marker ignored, `beat` doing nothing, `prune` deleting everything, `ops_jobs` dropping the clean-up, stop not recorded. |
| My own 14 earlier, across Phases 5.0–5.3 | All killed. |

Each mutation was restored and checked for equality. Product code is unchanged.

## Checks run

- `bash scripts/check.sh backend`: **832 passed**, Ruff clean, OpenAPI contract in sync. Exit code 0.
