# Review: Phase 3.3 whole-flow tests (counter to receipt)

- **Branch reviewed:** `feat/backend-phase3-flows` (54dcf0b, fixes in the follow-up commit)
- **Reviewer:** independent review agent (Opus 5.5), read-only, probing on a copy of the code
- **Date:** 2026-11-22

## Scope

`backend/tests/test_phase3_flows.py` adds five employee stories, run through the HTTP API with the real worker:

- walk-in to paid receipt, ending in the collections report
- a network retry
- a wrong amount voided and re-entered
- a fee lowered after payment
- a job handed over to a colleague

The counter-desk helpers moved to `tests/flow_helpers.py`, which the Phase 2 flows now share. Only tests changed; no product code.

## Findings

| ID | Severity | Finding | Outcome |
|---|---|---|---|
| M1 | Medium | The leaked-secret check matched only one spelling. A phone stored as typed ("+91 98765 43210") in the snapshot or audit would have passed, which the reviewer showed by mutating a copy of the code. | **Fixed.** The check now compares digits only. The same mutation is now caught. |
| L1 | Low | Rare flake at midnight in India: `voided_at` uses the wall clock, so it could fall on the next day from the test's "today". | **Fixed.** The report checks a window from yesterday to tomorrow. |
| L2 | Low | The print-order assertion had no ORDER BY. | **Fixed.** Rows are read ordered by id. |
| L3 | Low | The CANCELLED print didn't check its receipt number. | **Fixed.** |
| L4 | Low | The flows didn't check that an unassigned employee can't take a payment. | **Fixed.** Checked before and after the handover. |
| L5 | Low | `flow_helpers` only imported in pytest's default import mode. | **Fixed.** `pythonpath` now includes `tests`. Passes with `--import-mode=importlib` too. |

## Checked and found sound

- **Helper extraction:** the moved helpers are byte-for-byte identical, `run_worker` behaves as before, and the Phase 2 assertions are untouched.
- **Collection:** `flow_helpers.py` is never collected as a test, and nothing is collected twice.
- **Determinism:** receipt numbers come out the same every run, ties are broken by id, and the report's ordering is fixed.
- **Owner rules exercised:**
  - an amount above the balance is refused
  - only an admin can void
  - CANCELLED and DUPLICATE COPY appear on the right prints
  - fees are locked once money is received
  - completed jobs can still take payment
  - collections exclude voids and show `voids_in_period`
  - overpaid shows to the admin only
  - receipt access follows the job assignment
  - an idempotent replay returns the first payment, and a reused key returns 409
  - no reference number appears in the audit
- **Isolation:** each test rolls back, and the data is clearly fake.

## Checks

- **Full suite:** `scripts/check.sh backend` gives 574 passed, with the contract in sync (no contract change).
- **Deliberate breaks through the stories:** caught 11 of 11:
  - phone masking
  - receipt cancellation on void
  - the retry fingerprint
  - the fee lock
  - DUPLICATE marking
  - the due filter
  - the voided split in collections
  - receipt access
  - the balance check
  - the reference shown on the PDF
  - the M1 leak

  The first round missed the fee lock and the balance check; both stories were strengthened.
- **Incident during the checks:** one mutation script deleted `app/services/payment_service.py` from the working tree and lost its backup copy. The file had no uncommitted edits, so it was restored from the commit, and all app code was confirmed identical to it. Later mutations restore from git and verify the result.
