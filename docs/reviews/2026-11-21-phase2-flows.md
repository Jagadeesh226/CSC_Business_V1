# Review: Phase 2.5 whole-flow tests (employee side)

- **Branch reviewed:** `feat/backend-phase2-flows` (2b4f8b7, fixes in 3590ce8)
- **Reviewer:** independent review agent (Opus 5.5), read-only
- **Date:** 2026-11-21

## Scope

`backend/tests/test_phase2_flows.py` drives employee counter stories through the HTTP API, with the real background worker running inside each test's transaction:

- a new customer, from the first search to submission
- a returning customer who reuses a verified Aadhaar
- an unreadable scan that goes to manual review and is replaced
- employee limits and the admin override
- a job handed over to a colleague mid-flow (added after the review)
- a shared Aadhaar that is rejected (added after the review)

These are tests only: no product code or contract changes.

## Findings

| ID | Severity | Finding | Outcome |
|---|---|---|---|
| 1 | Medium | The worker's writes sat in a savepoint that a later rollback could undo. | **Fixed.** The helper commits before and after each tick. |
| 2 | Medium | The transaction clock never moves, so retried or backed-off tasks never ran, and how many tasks ran was not checked. | **Fixed.** Tasks scheduled "later" are made due, and `run_worker(expect=n)` asserts the count. |
| 3 | Medium | The OCR-overwrite check could not fail, because the OCR name equalled the customer name. | **Fixed.** OCR reads "Meena Devi" and the customer stays "Meena". No `customer_updated_from_ocr` entry is recorded. |
| 4 | Medium | Missing stories: OCR retry by an employee, preview and download audit, handover mid-flow, rejecting a shared document. | **Added** as two new flows. Items that accept several documents are already covered in `test_queues.py` and `test_documents.py`. |
| 5 | Low | The handler's `SET LOCAL` time limits stayed in force for the rest of the test. | **Fixed.** They are reset after each tick. |
| 6 | Low | The worker thread shares the test's connection. | **Documented.** This is safe because `run_one` waits for the thread to finish. |
| 7 | Low | The audit-leak check only looked for one form of the number. | **Fixed.** It now checks spaced, plain and partial forms. |
| 8, 9 | Low | Some step responses were not checked. | **Fixed.** Every step checks its status. |

## Checked and found sound

- **Handler failures:** a failing handler rolls back only its own savepoint.
- **Real worker paths:** the ownership check (`lock_owned`) and the after-commit Redis wake-up run for real.
- **Isolation:** no test depends on another test's order, there are no hard-coded IDs, and the personal data is clearly fake.
- **Product:** no product bugs were found.

## Checks

- **Full suite:** `scripts/check.sh backend` gives 488 passed, with the contract in sync.
- **Deliberate breaks:** the strengthened flows catch three breaks introduced on purpose:
  - OCR silently copying the name onto the customer
  - removing the reuse block on a shared document
  - recording a preview as a download
