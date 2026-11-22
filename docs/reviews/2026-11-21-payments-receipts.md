# Review: Phase 3.0 payments and receipts

- **Branch reviewed:** `feat/backend-payments` (f2890ae, fixes in b68e81c)
- **Reviewer:** independent review agent (Opus 5.5), read-only
- **Integration check:** Sonnet 5.5, over real HTTP
- **Date:** 2026-11-21

## Scope

- `POST /jobs/{id}/payments`, which takes an `Idempotency-Key` header
- `GET /jobs/{id}/payments`
- `POST /payments/{id}/void` (admin only)
- `payment_summary` on every job
- migration `ee3ab3e4c9e8`, which adds the receipts table and CHECK constraints on payments

## Findings

| ID | Severity | Finding | Outcome |
|---|---|---|---|
| M1 | Medium | A leftover import cycle: importing `app.schemas.fee` or `app.schemas.payment` first crashed. | **Fixed.** A test now imports each schema module on its own. |
| M2 | Medium | Every IntegrityError was reported as "Please retry". A receipt counter that fell behind existing receipts would block payments permanently. | **Fixed.** Errors are handled by constraint name. A reused key returns the original payment or 409. A receipt-number collision moves the counter past existing receipts and retries once. Anything else is logged and re-raised. |
| L1 | Low | A key lookup ran before the job and access checks, so a 409 could reveal that a key exists. | **Fixed.** Job and access are checked first, so the response is 404. |
| L2 | Low | The job lock was taken before checking access. | **Fixed.** Access is checked before the lock and again under it. |
| L3 | Low | A replay may return a payment that was voided since. | **Documented** in the route description. |
| L4 | Low | A blank reference was refused for cash. | **Fixed.** It is accepted and stored as none. |
| L5 | Low | "Already voided" returned 422. | **Changed** to 409, and documented. |
| L6 | Low | The migration did not pre-check bad legacy amounts or methods. | **Fixed.** It stops with a clear message, and the downgrade behaviour is documented. |
| L7 | Low | Test gaps. | **Added:** same key on two jobs at once, a counter that falls behind, key probing, a separate receipt counter. |
| L8 | Low | No endpoint shows a receipt, and `print_count` is unused. | **Planned for 3.1** (receipt PDF and search). |

## Integration check (real HTTP)

All 9 steps passed:

1. **Setup:** the fee total was 157.00 and the state was unpaid.
2. **Payment and retry:** a 201 with receipt `RCP-2026-000001`, a replay returned 200, and reusing the key for a different payment returned 409.
3. **Validation:** a UPI payment without a reference, a missing key and an amount above the balance were all refused with 422.
4. **Access:** an employee not assigned to the job got 404.
5. **Simultaneous requests:**
   - Five requests with the same key returned one 201 and four replayed 200s, and recorded one payment.
   - Two requests with different keys competing for the last 60.00 returned one 201 and one 422.
6. **Fees and voids:** the employee's fee was refused after payment and the employee's void returned 403. The admin's void cancelled the receipt, and a second void returned 409.
7. **Completed job:** a completed job still accepted a payment.
8. **Privacy:** the audit log holds no reference number or phone, and the receipt snapshot has the phone masked. The receipt keeps the transaction reference on purpose, because the customer needs it.
9. **Contract:** the changes are additions only.

## Checked and found sound

- **Lock order:** the job is locked before the payment, matching the fee rule.
- **Idempotency fingerprint:** amounts and notes are normalised before hashing, and the user's ID is part of it.
- **Arithmetic:** Decimal and NUMERIC are used throughout.
- **Snapshot:** built under the job lock.
- **Migration:** the upgrade, downgrade and upgrade round trip works, and `alembic check` is clean.
- **Status codes:** correct, with no SQL or stack traces in errors.

## Checks

- **Full suite:** `scripts/check.sh backend` gives 527 passed, with the contract in sync on the owner branch.
- **Deliberate breaks:** the tests caught 13 of 13. These included removing the job lock, the balance check, the fingerprint comparison, the replay access check, the cancelled-job check, receipt cancellation, the double-void check and phone masking, as well as undoing the M1, M2 and L1 fixes.
