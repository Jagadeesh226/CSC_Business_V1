# Review: Phase 4.2 vehicle expiry reminders, notifications and WhatsApp links

- **Branch reviewed:** `feat/backend-expiry-reminders`
  - Initial commit: 72ce870
  - Fixes: 9a6a806, plus one commit from the integration check
- **Reviewer:** independent review agent (Opus 5.5), read-only. It probed a copy of the code.
- **Integration check:** Sonnet 5.5, over real HTTP with the real worker, two workers at once.
- **Date:** 2026-11-25

## Scope

- **Reminders:** insurance, PUCC, fitness, tax or permit expiring from today to 30 days ahead (Asia/Kolkata) gets one reminder. The database makes it unique per vehicle, type and date.
- **Notification:** the responsible employee gets one notification per reminder.
- **Daily check:** runs once per business day from the worker's maintenance loop (`daily_runs` plus an advisory lock).
- **Expiry buckets:** expired, 0–7, 8–15, 16–30 and over 30 days.
- **WhatsApp:** click-to-chat links for valid Indian mobiles. Nothing is sent automatically, and done or dismissed is audited.
- **Notifications:** each user's own list.
- **Work-queue counts:** `reminders_to_send` and `unread_notifications`.

## Findings

| ID | Severity | Finding | Outcome |
|---|---|---|---|
| H1 | High | A vehicle assigned after its reminder existed (the normal flow for admin-created vehicles) never notified anyone. A reassignment didn't notify the new person either. | **Fixed.** A new responsible employee is notified of the vehicle's open reminders, the previous person's notifications are settled, and the daily check backfills any missing ones. |
| M1 | Medium | Landlines with an STD code (`080 2345 6789`, `0612 …`, `0712-…`) were treated as mobiles. That could send customer details to a stranger's WhatsApp. | **Fixed.** Numbers written with a leading 0 get no link. The reminder stays `pending` until a mobile number is added. |
| M2 | Medium | An expiry row's link could describe a different expiry. Rows more than 30 days out also got a link. | **Fixed.** Due rows use the vehicle's message, expired rows get their own, and rows over 30 days get none. |
| M3 | Medium | Race: a date change during the daily check could bring a stale reminder back. | **Fixed.** A reminder is created or reopened only if its date is still current, checked under a share lock on the vehicle. |
| L1 | Low | Other workers waited on the daily claim and logged timeouts. | **Fixed.** A non-waiting advisory lock means other workers simply skip. |
| L2 | Low | Titles read "expires in 0 days" and "1 days". | **Fixed.** |
| L3 | Low | The meaning of `reminders_to_send` was unclear. | **Documented:** all open reminders, both ready and pending. |
| L4 | Low | The pending and ready_to_send filters load every open reminder, and the bucket counts take 5 queries. | **Accepted** at centre scale. |
| L5 | Low | Mark-read endpoints returned untyped dicts. | **Fixed:** they now return `MarkedResponse`. |
| L6 | Low | A test left the daily job registered for later tests. | **Fixed:** it unregisters afterwards. |
| L7 | Low | Not tested: a done reminder staying done when its date comes back. | **Tested.** |

## Integration check (real HTTP, real worker)

All 8 steps passed:

1. A vehicle's dates gave exactly the insurance and PUCC reminders, both `ready_to_send`. The single WhatsApp message read "PUCC expires on … (5 days left); Insurance expires on … (20 days left) … - Integration CSC".
2. There were 2 notifications with readable titles, containing no phone or identifier values. The work-queue counts matched.
3. Buckets: expired 1, 0_7 1, 16_30 1, over_30 1. The over-30 row had no link, and the expired row's link was about its own expiry only.
4. Supersede, reopen, done and dismiss all behaved correctly, including 422 on repeats and a missing reason. Audit rows were written.
5. A landline customer's reminder was `pending` with no link. The first assignment notified that person, and a reassignment moved the notification to the new person.
6. Access: an employee without access saw nothing and got 404.
7. The real worker wrote the daily marker. Two workers at once wrote exactly one, with no duplicates and no error logs.
8. The contract change is additive.

**Fixes from integration:**
- A reminder replaced by a new date now settles its notification, and reopening makes it unread again.
- The reminder tests now ignore a `daily_runs` row left by a live run in the test database.

## Checked and found sound

- **Supersede query:** the correlated NOT EXISTS in the daily supersede is scoped to the outer reminder row.
- **Uniqueness:** unique constraints dedupe reminders, notifications per user and reminder, and daily runs.
- **Window and buckets:** inclusive and consistent with each other.
- **Links:** every character is URL-encoded. Messages and notifications carry no identifiers.
- **Access:** reminders and expiries follow vehicle access, and notifications are private.
- **Migration:** the round trip works and `alembic check` is clean.

## Checks

- **Full suite:** `scripts/check.sh backend` gives 680 passed, with the contract in sync on the owner branch.
- **Deliberate breaks:** every break was caught except one equivalent change: dropping the unique-constraint name from the skip-duplicates insert, which behaves identically.
  - The first round missed the same-day run, inactive employees and the bucket edges. Tests were added for each.
