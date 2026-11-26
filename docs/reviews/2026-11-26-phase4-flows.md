# Review: Phase 4.5 whole-flow tests for vehicles

- **Branch reviewed:** `feat/backend-phase4-flows` (244b13b, fixes in 3ad0072)
- **Reviewer:** independent review agent (Opus 5.5), read-only, running mutations on a copy of the code
- **Integration check:** not run. This step changes tests only and no product code, and these tests already drive the real worker.
- **Date:** 2026-11-26

## Scope

`backend/tests/test_phase4_flows.py` follows a vehicle through its life at the counter, end to end:

1. **RC to export:**
   - An uploaded RC is read by OCR, and the vehicle is created with no typing.
   - The lookup stops for a CAPTCHA, which the employee answers.
   - The dates are applied, a reminder and a notification are created, and the reminder's WhatsApp link is opened and marked done.
   - The Excel export follows, along with the audit trail.
2. **Renewal:** a new date retires the old reminder, and its history is kept.
3. **Doubtful RC and handover:**
   - Only an unsure field stops for a person.
   - Handing the vehicle to a colleague moves the notification and the right to reveal.
4. **Who sees what:** covers a colleague with an open job, a stranger, and a landline-only customer.

No product code, contract or dependency changes.

## Findings

| ID | Severity | Finding | Outcome |
|---|---|---|---|
| M1 | Medium | The stranger test had no reminder in it, so a reminders list missing its scope still passed. | **Fixed.** The test now creates a real reminder. The stranger gets 0 from `/reminders` and 404 when marking it done. |
| M2 | Medium | Secrets were checked only in the audit log. The application logs and the sealed CAPTCHA and state columns were never checked. | **Fixed.** The test now asserts that the chassis, engine, mobile and CAPTCHA answer are absent from the captured logs at DEBUG level. It also asserts that both sealed columns are cleared after the lookup. |
| L1 | Low | The doubtful-RC story did not show that the vehicle exists before the person resolves the unsure field, or that the queue empties afterwards. | **Fixed.** |
| L2 | Low | The export checks were weak: no status or content type, no engine mask, and `zip(strict=False)`. | **Fixed.** The test now checks the status and content type, `****4567`, and `strict=True`. It also checks that the full mobile appears in no cell (it appears only in the link). |
| L3 | Low | The lookup fixtures saved the task registry after importing the handler. The import registers the task, so the "restore" kept it, and the docstring said otherwise. | **Fixed** in both `test_phase4_flows.py` and `test_vehicle_lookups.py`. |
| L4 | Low | Two cases were untested: a landline-only customer's reminder, and a colleague whose only access is an open job. | **Fixed.** A landline customer's reminder is `pending` with no link. A colleague sees the vehicle masked and gets 403 on reveal. Once their job is completed, they get 404. |
| L5 | Low | The fake chassis number looked like a real manufacturer prefix. | **Fixed.** It is now `ZZ9FAKEVIN0123456`. |

## Mutation checks

Each mutation was run against the tests, then restored from git and checked for equality:

- **`/reminders` without the visibility scope:** caught by `test_who_sees_what`.
- **Marking a reminder done without the visibility check:** caught by `test_who_sees_what`.
- The reviewer's earlier mutations (unmasked export, lookup overwriting a later staff date, the handover not moving the reveal right) were caught by the original tests.

## Checks run

- `bash scripts/check.sh backend`: **723 passed**. Ruff is clean and the OpenAPI contract is in sync.

## Remaining (owner decisions, not this step)

- **Real VAHAN provider:** waits until the owner checks the portal terms.
- **Mobile-number lookup from VAHAN:** waits for the consent question under the DPDP Act and the requirements doc.
