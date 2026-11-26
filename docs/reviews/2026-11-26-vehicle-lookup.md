# Review: Phase 4.4 vehicle lookup adapter with employee-solved CAPTCHAs

- **Branch reviewed:** `feat/backend-vehicle-lookup` (6243e03, fixes in 5a4391d)
- **Reviewer:** independent review agent (Opus 5.5), read-only, probing a copy of the code
- **Integration check:** Sonnet 5.5, over real HTTP with the real worker
- **Date:** 2026-11-26

## Scope

- **Provider interface:** looks up validity dates and challans; may ask for a CAPTCHA, or fail.
  - Only a deterministic `fake` provider exists.
  - The VAHAN provider waits for the owner's check of the portal terms.
- **Lookup task:** `vehicle_expiry_lookup` (`POST /vehicles/{id}/lookups`).
  - At most one active lookup per vehicle.
- **CAPTCHA round trip:**
  - The image is served; a person types the answer, which starts the next round.
  - The provider state and the answer are sealed.
  - Unanswered CAPTCHAs expire after 5 minutes.
- **Results:** stored with their retrieval time.
- **Failures:** recorded with fixed, safe reasons.
- **Audit:** every step is audited.
- **Work queue:** gains `lookups_waiting_for_captcha`.

## Findings

| ID | Severity | Finding | Outcome |
|---|---|---|---|
| H1 | High | A lookup silently replaced dates entered by staff or read from documents, even with older dates. A renewed policy could revert to "expires in 11 days" and trigger wrong reminders. | **Fixed.** Lookup dates apply only when there is no date, the current one came from a lookup, or the found date is later. Anything else is listed in `not_applied` for a person. |
| H2 | High | If the failure hook failed, a lookup stayed `queued` forever and blocked that vehicle for good (409). | **Fixed.** A maintenance job fails `queued` lookups whose task has ended or vanished. |
| M1 | Medium | CAPTCHA expiry was not audited. | **Fixed.** |
| M2 | Medium | Design gaps for a real browser provider: the same answer was resent after a temporary error; wrong answers were unlimited; a timed-out attempt could run alongside its retry. | **Fixed:** a busy portal after an answer ends the lookup; rounds are capped at 5. **Documented:** the provider must be safe to call twice and keep any browser session outside the task. |
| L1 | Low | Schema names were generic. | **Renamed** to `VehicleLookup*`. |
| L2 | Low | A new required field on `WorkQueueSummary`. | **For the owner** to approve in the contract PR. |
| L3 | Low | Lookup results had no person attached. | **Fixed.** The requester is recorded on applied dates and the audit. |
| L4 | Low | A CAPTCHA could be answered after the vehicle was deactivated. | **Fixed.** It is refused. |
| L5 | Low | The waiting count included vehicles the person can no longer see. | **Fixed.** |
| L6 | Low | The task stayed registered for the whole test run, and tests were missing. | **Fixed.** Registration is module-scoped, and tests were added for every fix: final failure after retries, stranger 404s, stuck recovery, the round cap, inactive vehicles, and the waiting count after losing access. |

## Integration check (real HTTP, real worker)

Six steps passed and one optional step was skipped.

1. **Plain lookup:**
   - Dates applied with source `lookup`.
   - Challan status set to pending, with the time it was checked.
   - Audit entries recorded in the requester's name.
   - Starting a second lookup while one was queued returned 409.
2. **CAPTCHA:**
   - The image came back as PNG with `no-store`.
   - A wrong answer led to a new CAPTCHA; the right answer completed the lookup.
   - Answers never appear in the audit log.
   - A stranger got 404.
3. **Not found:** recorded as failed. A busy portal was retried and then failed after 3 attempts.
4. **Staff date kept:** a date entered by staff at +360 days was kept. The lookup listed insurance in `not_applied` and applied only PUCC.
5. **Contract:** additions only. `WorkQueueSummary` gains one required field.

## Checked and found sound

- **Locking:** lock order is vehicle then lookup, with no deadlocks. Two answers at once are serialised.
- **Stale rounds:** a stale round never calls the provider.
- **Retries:** the same lookup row is reused across retries.
- **Access:** 404 everywhere for vehicles the user can't see.
- **Sealed data:** state and answers are sealed and cleared once the lookup ends. The CAPTCHA image is stored in clear, which is fine because it is not customer data.
- **Shared helper:** `record_expiry` keeps the previous `set_expiry` behaviour.
- **Migration:** the round trip and `alembic check` are clean.

## Checks

- **Test suite:** `scripts/check.sh backend` gives 719 passed, with the contract in sync on the owner branch.
- **Deliberate breaks:** every one was caught after strengthening two tests: the stale-round provider call, and the waiting count after losing access.
