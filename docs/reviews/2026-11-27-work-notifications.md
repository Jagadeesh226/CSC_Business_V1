# Review: Phase 5.1 work notifications

- **Branch reviewed:** `feat/backend-work-notifications` (e168e3b, fixes in 587d1d1 and 7a05617)
- **Reviewer:** independent review agent (Opus 5.5), read-only, running mutations on a copy of the code
- **Integration check:** Sonnet 5.5, over real HTTP with the real worker
- **Date:** 2026-11-27

## Scope

In-app notifications that tell the right person, once, when work needs them. Each one is settled (marked read, never deleted) when it no longer does.

| Kind | Who is told | Settled when |
|---|---|---|
| `job_assigned` | The person someone else assigned to a job (never for assigning yourself). | They are taken off the job, the job closes, or they are deactivated. |
| `ocr_failed` | The uploader, if they can still check the document. Otherwise the people on its open jobs, or nobody: it waits in the review queue. | The document is verified, rejected, replaced, archived or re-run. Also, for anyone who can no longer act on it, when the job closes or they leave it. |
| `lookup_captcha` | Whoever started the vehicle lookup, once per lookup. | The CAPTCHA is answered, or the lookup completes, fails or expires. |
| `task_failed` | Active admins, for system failures only (retries used up, timeouts, no handler). One notice per task type per hour in IST, counting the failures and made unread again on each new one. | Read by the admin. |

Changes behind it:

- **Data:** `notifications` gains `event_key` (unique per user), link ids, `task_type` and `occurrences`, plus four new kinds. Migration `d7e9f0a1b2c3`.
- **API:** five optional fields on `NotificationRead`.

## Findings

| ID | Severity | Finding | Outcome |
|---|---|---|---|
| M-1 | Medium | `ocr_failed` went to the uploader with no access check. An employee taken off the job got "Could not read: Aadhaar card for &lt;customer&gt;" for a document that returned 404 for her, or one she could not review. If the uploader was inactive, nobody who could act was told. | **Fixed.** The notice goes to someone who can act: an admin, or someone assigned to an open job using the document. If the uploader cannot act, it goes to the people on its open jobs. If nobody can, it waits in the review queue. |
| M-2 | Medium | Every expected outcome raised an admin alert: an unreadable scan, or a vehicle that was not found. | **Fixed.** Admins are told only about system failures. A `PermanentTaskError` is the business of the person its failure hook reports to. The "no handler" case still alerts. |
| M-3 | Medium | `ocr_failed` stayed unread after the job closed or the person left it, although they could no longer act on it. | **Fixed.** Both now settle it for people who can no longer act. Found while fixing it: with autoflush off, `job_closed` checked the job's old status, so nothing settled. It now flushes first. |
| L-1 | Low | Deactivating a user settled nothing. | **Fixed.** Deactivation settles all of the user's work notices. |
| L-2 | Low | Re-running OCR did not settle the old notice. A second failure was not announced, because the key was the same. | **Fixed.** A retry settles the old notice, and the key now includes the task and attempt. |
| L-3 | Low | Admins were selected without an order, so two workers could deadlock. | **Fixed.** They are selected in id order. |
| L-4 | Low | Read notices stay listed after the user loses access, for example "PAN card application for Kavya Rao". | **For the owner.** Kept as is for now. They saw it while they were on the job. |
| L-5 | Low | Only round 1 of a CAPTCHA is announced, only to whoever started the lookup. | **Kept as designed.** Later rounds follow an answer someone has just typed. |
| L-6 | Low | Test gaps: 11 of the reviewer's 23 mutations survived. | **Fixed.** See below. |

Found by the integration check: the admin notice said "Since 00:00" with no time zone. It now says IST.

## Mutation checks

After the fixes, 27 mutations were run against `tests/test_work_notifications.py`, covering:

- **Recipients:** no self-assign check; ignoring who can act; no hand-over to the job; ignoring closed jobs; inactive users told; employees or inactive admins getting admin alerts; admins alerted about permanent errors.
- **Settling:** each settle path removed in turn; settling everyone on unassign; no read filter in settle; no flush before close.
- **Duplicates and counts:** no re-alert after retry; a CAPTCHA notice per round; no hour bucket; no unread on repeat.
- **Isolation:** an unguarded admin notice.

All 27 were caught. Each file was restored from an in-memory copy and checked for equality.

Two code paths are covered only indirectly: settling on lookup completion, and settling on lookup failure. In practice the CAPTCHA answer settles the notice first. Both calls are kept as a safety net.

## Integration check (real HTTP, real worker)

All 9 checks passed:

1. **Assignment:** told only to the person assigned.
2. **Unassignment:** settles their notice.
3. **Unreadable scan:** told to the uploader; admins are not alerted.
4. **Rejection:** settles the failed-reading notice.
5. **Retries used up:** each active admin gets one "1 document reading (OCR) task failed"; employees get none.
6. **CAPTCHA:** told to the requester; answering settles it and the lookup completes.
7. **Deactivation:** settles everything for that user.
8. **Privacy:** another user's notification returns 404.
9. **No contents:** no Aadhaar number or OCR text appears in any notification.

## Checks run

- `bash scripts/check.sh backend`: **782 passed**, Ruff clean. The contract check passes once the contract PR merges.
- The contract diff is additions only: five optional fields on `NotificationRead`.
- Migration: down and up both run, and `alembic check` reports no drift.
