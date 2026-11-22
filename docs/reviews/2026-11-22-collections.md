# Review: Phase 3.2 money in queues and the collections report

- **Branch reviewed:** `feat/backend-collections` (9db90dc, fixes in 0e88943)
- **Reviewer:** independent review agent (Opus 5.5), read-only
- **Integration check:** Sonnet 5.5, over real HTTP
- **Date:** 2026-11-22

## Scope

- `GET /jobs?payment=due|paid|overpaid`
- Work queue: `jobs_with_balance_due`, plus `jobs_overpaid` for admins only
- Admin-only `GET /reports/collections`
- Migration that adds the `ix_payments_received_at` index

## Findings

| ID | Severity | Finding | Outcome |
|---|---|---|---|
| M1 | Medium | Voiding an old payment changes a past day's total, and the void appears on no day, so a signed-off cash count stops matching. | **Fixed.** New `voids_in_period` (keyed on `voided_at`) shows every void made in the period. The note says the figures are "as known now". Covered by a test. |
| M2 | Medium | A far-future date (9999-12-31) caused a 500 from date overflow. | **Fixed.** Dates must fall between 2000-01-01 and 2099-12-31, otherwise 422. |
| L1 | Low | The payment filter uses correlated SUM subqueries, and the work queue now runs about 10 counts. | **Deferred.** Fine at CSC scale; can move to grouped joins or cached totals when history grows. |
| L2 | Low | `by_receiver` order was not fixed when totals tied. Null receivers were untested. | **Fixed.** Ties sort by name, then id, with unknown receivers last. Tested. |
| L3 | Low | A cancelled job that still holds money appears in no queue. | **Owner decision.** Money filters exclude cancelled jobs, and refunds are out of Phase 3. |
| L4 | Low | `label` and `note` were optional in the generated schema. | **Fixed.** Both are now required. |
| L5 | Low | Viewing the report is not audited. | **Owner decision.** The rule audits exports; this is an on-screen report. |
| L6 | Low | Test gaps. | **Added:** later void, date range, receiver ties and null receiver, money with no fees, filter matching the job summary, employee overpaid scope. |

A new test also found that an empty void total showed as `"0"`; it now shows `"0.00"`.

## Integration check (real HTTP)

All six steps passed:

- **Filter:** `?payment=` gave due A and B, paid C, overpaid D. The cancelled job was excluded. Each result matched that job's `payment_summary.state`, and employee 2 saw nothing.
- **Work queue:** the employee saw a balance due of 2 and a null overpaid count. The admin saw an overpaid count of 1.
- **Collections:**
  - Labelled "Collections" with a "not profit" note.
  - Total 270.00: cash 170, UPI 100, split correctly by receiver.
  - After a void, the total fell to 230.00, with voided 1/40.00 and voids_in_period 1/40.00.
  - Access: employee 403, anonymous 401.
  - Bad date ranges gave 422.
- **Contract:** additions only, compared as JSON.
  - New `payment` query parameter on `GET /jobs`.
  - Two new `WorkQueueSummary` fields.
  - New path `/reports/collections`.
  - Six new schemas.

## Checked and found sound

- **Filter and job summary always agree:** both use only active fees and only received payments. A job with no fees matches no filter.
- **Day boundaries:** days are measured in Asia/Kolkata time, and the date bounds can use the index.
- **Sums:** totals use exact decimals.
- **Access:** employees see only their assigned jobs, and the report is admin-only.
- **Migration:** upgrade, downgrade and re-upgrade succeed, and `alembic check` is clean.

## Checks

- **Full suite:** `scripts/check.sh backend` gives 569 passed, with the contract in sync on the owner branch.
- **Deliberate breaks:** the tests caught 11 of 11. These covered:
  - the cancelled-job and voided-payment rules
  - the paid definition
  - admin-only overpaid
  - the voided split, period limits and day boundary
  - the M1, M2 and L2 fixes
