# Review: OCR and review (Phase 2.3)

- **Branch:** `feat/backend-ocr-review` at `29d9963`
- **Diff:** `git diff origin/main...feat/backend-ocr-review` (backend only: `ocr_results` model and migration `c1d2e3f4a5b6`, `services/integration/ocr.py`, `workers/ocr_handler.py`, `services/ocr_service.py`, OCR routes and schemas, `storage.seal/unseal`, `document_service` split into `apply_verify`/`apply_reject`, worker failure hook, tests)
- **Reviewed against:** `AGENTS.md`, `backend/AGENTS.md`, `docs/contracts/decisions.md` §8 and §10, `docs/contracts/permissions.md` §4, the review prompt in `docs/SETUP-AI-WORKFLOW.md` §5, earlier reviews (processing engine, documents and checklists), requirements §3.2, FR-07/FR-08 and §6, and the owner's Phase 2 decisions.
- **Reviewer:** review agent. Findings only; no application code was changed.

## Checks run

- `TEST_DATABASE_URL=... uv run pytest -q` (test database `csc_crm_test` only): **453 passed**.
- `bash scripts/check.sh backend` on the branch: dependency lock, Ruff lint, Ruff format and pytest (453 passed) **PASS**. The contract check **FAILS (contract drift)**. The generated spec only adds three paths (`/api/v1/documents/{document_id}/ocr`, `.../ocr/review`, `.../ocr/retry`) and nine schemas (`ApplyToCustomer`, `CustomerField`, `FieldFlag`, `OcrFieldRead`, `OcrRead`, `OcrReviewRequest`, `OcrReviewResponse`, `OcrStatus`, `ReviewDecision`). No existing path or schema changes.
- Migration round trip on the test database: `alembic downgrade b9c0d1e2f3a4` exit 0, then `alembic upgrade head` exit 0. `alembic check` reports "No new upgrade operations detected."
- `bash scripts/check_ownership.sh origin/main` on this review branch: `Ownership check passed for 'feat/review-ocr-review'.`
- Probes (`scratchpad/ocr-review/probe.py`) used the test database with real commits, a throwaway storage directory and key, and in-memory Redis. Afterwards every table was truncated (documents 0, tasks 0), no session was left `idle in transaction`, and the storage directory was deleted.
  1. **Final attempt lost to lease expiry.** The task was set to `running`, `attempts = max_attempts = 3`, with an expired lease, and then `Worker.requeue_expired()` ran. Result: task `failed` ("Timed out: the worker did not finish in time."), **document still `processing`**, no `ocr_results` row, and `GET /ocr` reports `pending`. `POST /ocr/retry` returned **409** "OCR has not failed for this version", `verify` returned **422** and `reject` returned **422** (see H-1).
  2. **Final attempt times out while the handler holds the document lock.** Setup: `max_attempts=1`, `timeout_seconds=2`, the provider sleeps 1.7 s, and the handler sleeps 0.8 s after `lock_document`. The worker's timeout path ran the failure hook, which skipped the locked document (`SKIP LOCKED`). The late handler then failed the ownership check and rolled back. Result: task `failed` ("Timed out."), **document `processing`**, no result, and retry, verify and reject refused as in probe 1 (see H-1).
  3. **Admin task retry after the failure hook.** An "unsupported" file gave a failed result, and the document moved to `needs_review`. Then `POST /tasks/{id}/retry` was called and the worker ran again. The task became **`succeeded`** with result `{"skipped": "stale"}`, and the OCR result was still `failed`. After that, `POST /documents/{id}/ocr/retry` returned **409** "The OCR task is not in a failed state." (see M-1).
  4. **Document type gets OCR fields after upload (same state as a pre-2.3 document).** The document was `uploaded` with no task at all, yet `GET /ocr` reported `ocr_status: "pending"`, and retry returned 409 (see L-1).
  5. `OcrReviewRequest` validation error for a rejection without a reason: pydantic's error `input` contains the **full submitted Aadhaar number**, and FastAPI 0.150's default 422 handler returns that `input` (see L-2).

## Critical

None.

## High

### H-1: A document can stay in `processing` for good. GET reports `pending`, and the document-level retry, verify and reject all refuse it

- **File:** `backend/app/repositories/task_repository.py:94-114` (`requeue_expired` marks tasks `failed` but never runs `on_final_failure`); `backend/app/workers/ocr_handler.py:136-150` (`on_ocr_failed` uses `with_for_update(skip_locked=True)` and returns if the row is locked); `backend/app/workers/worker.py:262-301` (the hook runs inside the transaction that holds the task row lock; hook exceptions are logged and swallowed); `backend/app/services/ocr_service.py:260-282` (`retry` needs a `failed` result row **and** a `needs_review` document); `backend/app/services/ocr_service.py:74-81` (`_read`: no result means `PENDING`).
- **What is wrong:** The only thing that moves a document out of `processing` after OCR gives up is `on_ocr_failed`. Three final-failure paths bypass it:
  1. **Lease expiry.** If the worker process dies (OOM on a large file, SIGKILL after the stop grace period, host restart) during the last attempt, another worker's `requeue_expired` marks the task `failed` in SQL and no hook runs.
  2. **Timeout race.** `_record_failure` locks the task row and then calls the hook, which skips a document row that is locked. The row is locked when the timed-out handler is between `lock_document` and `lock_owned`, which is exactly where it waits for the task row the main thread holds. It is also locked when an API request (verify, link, retry) holds it at that moment.
  3. **Hook exception.** Any exception in the hook is rolled back to the savepoint and only logged.

  In every case the task is `failed` but there is no `ocr_results` row. `ocr_service.retry` then refuses the document ("OCR has not failed for this version"). `apply_verify` and `apply_reject` refuse `processing`, and the OCR review refuses anything but `needs_review`.
- **How to trigger:** Probes 1 and 2. Probe 1 is a worker killed during the third attempt. In production that happens deterministically when a file crashes or OOMs the OCR engine three times in a row.
- **Impact:** The document is blocked from the whole employee workflow. Its single-document checklist item stays `pending`, so the job cannot be completed. `GET /ocr` says `pending`, so nobody is told OCR has given up. The only ways out are an admin finding the task in the generic task list and retrying it, or someone with review rights uploading a new version of the same file. This conflicts with decisions.md §10 ("Failed tasks must be recoverable and observable") and the owner's Phase 2 rule that a failed OCR goes to manual review.
- **Suggested fix:**
  1. Run the type's `on_final_failure` for every row that `requeue_expired` moves to `failed`. `RETURNING task_type, payload` gives what is needed, and the hook can run in a per-row savepoint.
  2. Do not rely on `SKIP LOCKED` while the task row is locked. Commit the task's `failed` status first. Then run the hook in a new transaction that waits for the document lock (with a `lock_timeout`). By then the late handler's `lock_owned` returns `None` and it rolls back, releasing the document. Alternatively, the hook can be retried later (see point 3).
  3. Make the document-level state self-healing: `ocr_service.retry` (and `_read`) should treat "document `processing`, current-version task `failed`, no completed result" as failed OCR. That gives `ocr_status: failed`, and retry allowed.
  4. Add worker-level tests for each path: lease expiry on the last attempt, a timeout while the handler holds the document lock, and a hook that raises. Today `on_ocr_failed` is only called directly (`tests/test_ocr.py:265-289`).

## Medium

### M-1: An admin retry of a failed `document_ocr` task does nothing, and afterwards the document-level retry is impossible

- **File:** `backend/app/services/task_service.py:47-76` (`retry_task` requeues any failed task), `backend/app/workers/ocr_handler.py:50-57, 75-76` (`_is_current` requires `processing`; otherwise `{"skipped": "stale"}` and the task succeeds), `backend/app/services/ocr_service.py:281-282` (retry requires the task to be `failed`)
- **What is wrong:** Once the failure hook has moved the document to `needs_review` with a failed result, the generic admin retry requeues the task but leaves the document in `needs_review`. The handler then sees a "stale" document and returns success without running OCR. The task is now `succeeded`, so `POST /documents/{id}/ocr/retry` refuses for good ("The OCR task is not in a failed state.").
- **How to trigger:** Probe 3.
- **Impact:** An admin who uses the task screen to recover a failed OCR (the obvious recovery path, and the one that H-1 currently relies on) gets a green "succeeded" while nothing ran. After that, the version can never be OCR'd again except by uploading a new version. Task status and document state disagree, which hurts "observable".
- **Suggested fix:** Give each task type a say in retries. For example, add a registry `before_retry` hook that, for `document_ocr`, applies the same checks and the same `document → processing` move as `ocr_service.retry`. Or have `retry_task` refuse `document_ocr` and point to the document retry. Also let `ocr_service.retry` requeue a task that is `succeeded` with a `skipped` result while the current result is `failed`. Add a test for "admin retry after the hook".

## Low

### L-1: `ocr_status` comes only from the result row, so it reports `pending` with no task and `failed` while OCR is running again

- **File:** `backend/app/services/ocr_service.py:74-82`
- **What is wrong:** No result row means `pending`, whatever the task or the document state. Documents uploaded before 2.3, documents whose type gained OCR fields later, and the H-1 states all show `pending` with no task behind them (probe 4). Retry refuses those (409), so their OCR can never start. After `POST /ocr/retry`, the response and GET show `ocr_status: failed` (the old row) while the document is `processing`.
- **Suggested fix:** Derive the status from the document status and the current-version task as well: `processing` with a queued or running task is pending, a failed task is failed, and no task is `not_started`. Optionally let retry (or a "run OCR" action) queue a first task for a version that never had one.

### L-2: A 422 on the review request echoes the submitted Aadhaar/PAN values

- **File:** `backend/app/schemas/ocr.py:75-94` (model-level validator); `backend/app/main.py` (no `RequestValidationError` handler, so FastAPI's default is used)
- **What is wrong:** Every model-level error (rejection without a reason, a rejection with `apply_to_customer`, a duplicate target, an extra key) and every field error (a value over 500 characters) is returned with pydantic's `input`. For the model validator, `input` is the whole body, including every confirmed value in full (probe 5).
- **Impact:** The value goes back only to the person who sent it, but full identity numbers end up in error responses, browser consoles and any front-end or proxy error logging. permissions.md §4 says to mask sensitive values in general-purpose responses.
- **Suggested fix:** Add an app-wide `RequestValidationError` handler that drops `input` and `ctx` (keep `loc`, `msg`, `type`), or at least do this for `/documents/*/ocr/review`. Add a test that a 422 body does not contain the submitted number.

### L-3: The fake provider is the production default and trusts a script embedded in any uploaded file

- **File:** `backend/app/services/integration/ocr.py:58-108` (`OCR_PROVIDER` defaults to `fake`; `FakeOcrProvider` reads `FAKEOCR:{json}` from the file bytes)
- **What is wrong:** With no configuration, every real customer document is decrypted and given to the fake provider. Its docstring says it is "for development and tests only; never reads real documents". A file that contains the marker gets whatever values and confidences it declares, shown as `ok` (high confidence) on the review screen. A misspelt `OCR_PROVIDER` is only noticed when every document fails permanently with "Unknown OCR provider".
- **Impact:** This is low today, because every value still needs a human review. But "OCR looks confident" is exactly what reviewers learn to trust.
- **Suggested fix:** Require `OCR_PROVIDER` to be set explicitly, allow `fake` only together with an explicit development or test flag, and validate the provider when the worker starts. Until a real provider exists, a `none` provider that returns every field as missing is the honest production default.

### L-4: Provider adapter readiness: verbatim error messages, no deadline, transaction held during the call

- **File:** `backend/app/workers/ocr_handler.py:85-92`, `:159`, `:167`; `backend/app/services/ocr_service.py:123`
- **What is wrong:**
  1. `TaskError(str(exc))` copies the adapter's exception message into `background_tasks.last_error`, `ocr_results.failure_reason` (shown to **every** document viewer) and the `document_ocr_failed` audit. The rule that "messages must not contain document content" is only a comment. An adapter written as `raise OcrTemporaryError(str(sdk_error))` would leak provider error text, which can quote the request.
  2. The provider gets no deadline and no size or page hint. The handler calls it while its database transaction is open (idle, no row locks) for up to the 180 s task timeout, holding a pooled connection and an old snapshot.
- **Suggested fix:** Map adapter exceptions to a fixed set of safe codes or messages in the handler (for example `"temporary"`/`"unsupported"` plus a fixed message), and never `str(exc)`. Pass a deadline (`info` timeout minus margin) to `extract`, and require real adapters to apply it to HTTP and subprocess calls. End the read transaction (`db.rollback()` after loading `stored` and `specs`) before calling the provider; the code already re-checks under the lock afterwards.

### L-5: Masking is opt-in per field and follows the live document-type settings, also for results stored earlier

- **File:** `backend/app/workers/ocr_handler.py:40-47` (`field_specs` reads the current `DocumentType.ocr_fields`); `backend/app/schemas/catalogue.py` (`OcrField.sensitive` defaults to `False`); `backend/app/services/ocr_service.py:74-127`
- **What is wrong:** An admin who defines `aadhaar_number` on a type marked `sensitivity: sensitive` but forgets the per-field flag exposes full Aadhaar numbers to every document viewer. Editing a type later changes how already-stored results are shown. Turning the flag off unmasks old results, and renaming a key hides its stored value and makes the review require the new key.
- **Suggested fix:** Mask by default every field of a `sensitive` document type, or reject such a type unless each field states `sensitive` explicitly. Snapshot the field specs (key, label, sensitive) into `ocr_results` when OCR runs, and use the snapshot for display and review of that result.

### L-6: Plain `POST /verify` still verifies a document that is waiting for OCR review, and the OCR result then stays unreviewed

- **File:** `backend/app/services/document_service.py:479-495` (`apply_verify` accepts `needs_review`); `backend/app/services/ocr_service.py:197-201` (the review needs `needs_review`)
- **What is wrong:** After OCR, an employee can skip the field review by using the old verify button. Once the document is verified, the OCR review is refused, so the result is never confirmed (`reviewed_by` stays null). FR-08 and §6 expect values to be confirmed ("mark the result reviewed").
- **Impact:** No customer data changes (verify alone applies nothing), so this is a workflow gap, not a data risk.
- **Suggested fix:** Decide with the owner. Either plain verify is refused while the current version has a completed, unreviewed OCR result (with an error that points to the OCR review), or keep it and document that the OCR review is optional.

### L-7: The OCR retry skips the closed-job rule and does not stamp `updated_at`

- **File:** `backend/app/services/ocr_service.py:260-299`
- **What is wrong:** Verify, reject and new version all call `_ensure_no_closed_job` for employees; retry does not. An employee still assigned to a completed job can re-run OCR and move that job's document `needs_review → processing → needs_review`. The task's `updated_at` is not set, unlike in `task_service.retry_task`.
- **Suggested fix:** Call `document_service._ensure_no_closed_job(jobs, actor, "process")` (keep `jobs` from `lock_for_write`), and set `task.updated_at` to the same `clock_timestamp()`.

### L-8: Both sealed columns use the same associated data

- **File:** `backend/app/workers/ocr_handler.py:36-37` (`sealed_context`), used for both `extraction_sealed` and `review_sealed` (`backend/app/services/ocr_service.py:63-71, 225`)
- **What is wrong:** The associated data binds a blob to its document and version but not to its column. Swapping `extraction_sealed` and `review_sealed` in the database (or copying one over the other) still decrypts, so confirmed values could be shown as "what OCR read", or the other way round.
- **Suggested fix:** Bind the column too, for example `ocr:<doc>:v<n>:extraction` and `...:review`. This needs no migration while no real data exists.

### L-9: Reading OCR fields is not audited, while reading the document is

- **File:** `backend/app/services/ocr_service.py:131-144`
- **What is wrong:** `GET /documents/{id}` records `document_viewed`, but `GET /documents/{id}/ocr` without `reveal` records nothing. Yet it returns the name, date of birth and address read from an identity document. A reveal attempt refused with 403 is not audited either, unlike `document_access_denied`.
- **Suggested fix:** Record `ocr_viewed` (version only) for every read, and `ocr_reveal_denied` for a refused reveal.

### L-10: Small efficiency and dead-code points

- `backend/app/services/ocr_service.py:236-244`: the `corrected` list decrypts and parses `extraction_sealed` once per field (up to 30 AES-GCM decryptions and JSON parses per review). Open it once before the loop.
- `backend/app/workers/ocr_handler.py:77-79`: the `already_done` branch cannot be reached. A completed result always comes with `needs_review` in the same commit, and `_is_current` returns earlier. The test at `tests/test_ocr.py:236-237` exercises only the stale branch.
- `backend/migrations/versions/c1d2e3f4a5b6_ocr_results.py:69-71`: `ix_ocr_results_document_id` duplicates the leading column of `uq_ocr_results_document_version`. Drop `index=True` before this migration is applied anywhere, or in a later migration.

### L-11: Test gaps

- **File:** `backend/tests/test_ocr.py`
- The worker paths that matter most are untested. These are the failure hook through `_record_failure` (savepoint, `SKIP LOCKED`), lease expiry on the last attempt, and a hook that raises. All three are behind H-1. The only end-to-end test covers the happy path.
- `run_ocr` calls the handler directly, so the ownership check and the `HandlerSession` commit guard are not part of the OCR tests.
- There are no tests for these: retry refused for viewers (403), archived documents, a running task (409), the `ocr_retried` audit, and the closed-job behaviour of review and retry.
- No test checks that a provider error message, `failure_reason`, `last_error` or a 422 body carries no field values (see L-2 and L-4).
- `GET /documents/{id}/ocr` can return 403 (`reveal`), but the route declares only `NOT_FOUND` in its OpenAPI responses (`backend/app/api/v1/routes/documents.py:208`).

## Accepted gaps

- **Contract drift:** three new paths and nine new schemas, additive only. The owner must review, regenerate and commit `docs/contracts/openapi.json` (`backend/AGENTS.md`, "API Contract"). The requirements sketch named `POST /documents/{id}/ocr` and `/ocr-review`. The branch uses `GET /ocr`, `POST /ocr/review` and `POST /ocr/retry`, which the owner should confirm.
- **Fake provider until the owner chooses one,** after testing on their own samples and a legal/privacy review of cloud processing of Aadhaar. See L-3 for the default.
- **A stopped worker leaves new OCR documents in `processing`,** and they cannot be verified or rejected until it runs. This is visible in the admin task list. It is by design, provided H-1 is fixed so that final failures always reach manual review.
- **Customers have no "verified field" concept.** Changes reach the customer only through the explicit per-field mapping in a `verify` review, and are audited by field name without old values, the same as `customer_updated`. Restoring an earlier value relies on the audit trail plus backups.
- **One `ocr_results` row per document version.** A retry overwrites a failed result, and the history of attempts is in `audit_logs` and `background_tasks` only. Requirements §6 mentions "OCR result history". Confirmed values are kept separately (`review_sealed`), which covers corrections.
- **Apply-to-customer covers name, phone, email and address only.** Job and vehicle fields (requirements §6) come with those features.
- **`seal` uses `DOCUMENT_ENCRYPTION_KEY`,** the same key as the files, with no key id, so rotation needs a re-encryption job. If unsealing fails (wrong key, corrupt row), the request returns a 500 without details.

## Checked and correct

- **No plaintext identity data at rest or in transit to logs.** `confidences` holds field keys and numbers only. Task payloads hold ids, task results hold counts, and audit details hold keys, version, decision and field names only. The worker logs exception type names only. Values and raw text are sealed with AES-GCM, a random 12-byte nonce and the document and version as associated data. The tests assert that the Aadhaar number and name bytes are absent from both blobs.
- **Masking and reveal.** Sensitive fields are masked to the last four characters, both `value` and `reviewed_value`. `raw_text` is returned only with `reveal`. A reveal needs review rights (assigned to a linked job, or admin), a viewer gets 403, and `ocr_sensitive_viewed` is committed before the data is returned. The review response is always built unrevealed.
- **Explicit review workflow.** OCR output never reaches the customer by itself. Only a `verify` review with an explicit `apply_to_customer` mapping copies values, and those are the reviewer's confirmed values, not raw OCR. A rejection cannot apply values. Every field must be confirmed. Unknown fields and mappings are refused, and customer values are validated through `CustomerUpdate` before anything changes. Service errors name fields, never values. The whole review is one commit.
- **Review and view rights.** Review, reveal and retry call `ensure_can_review`, and the review goes through `apply_verify`/`apply_reject`, which keep the closed-job rule for employees. Viewers on another open job of the customer can read masked fields but cannot reveal or review (tested).
- **Lock order.** API writes take jobs, then the document, then (retry only) the task. The handler takes the document, then the task. The worker takes the task, then the document with `SKIP LOCKED`. Claim and `requeue_expired` take task rows only. There is no cycle, so there is no deadlock; the cost is the skipped hook in H-1. The handler re-checks the document under its lock with `populate_existing`. The provider is called with no row lock held.
- **Stale and duplicate handling.** A new version, archive or review makes an older task return `skipped: stale` (probe 3 and tests). `uq_ocr_results_document_version` protects against double inserts. Two reviews are serialised by the document lock, and the second gets 422. Review (`needs_review`) and the handler (`processing`) never act on the same state. Retry against a running task returns 409.
- **Enqueue.** The task is inserted in the upload or new-version transaction with key `document_ocr:<doc>:v<version>`, and the wake-up is published only after commit (end-to-end test, `fake_redis.xlen == 1`). Attempts stay monotonic on retry, which keeps the processing-engine M-2 fix intact.
- **Failure hook isolation.** The hook runs in a savepoint, so a failing hook cannot lose the task's `failed` status.
- **Migration** `c1d2e3f4a5b6`: it round-trips on the test database, matches the models (`alembic check` clean), uses integer keys, and has named constraints for status and uniqueness.
- **Scope.** The diff touches `backend/` only. No dependencies were added (`uv lock --check` passes). No contract, `AGENTS.md` or owner files were edited. No tests were weakened. The existing-test edits restore the real `document_ocr` handler after the engine tests' fake one, and give the document-lifecycle tests' Aadhaar type no OCR fields so they keep testing the non-OCR lifecycle; the OCR lifecycle is covered in `test_ocr.py`.

## Verdict

**CHANGES REQUESTED.** H-1 must be fixed, with worker-level tests for each path. M-1 should be fixed in the same change, since admin task retry is the recovery path H-1 leaves. The owner needs to approve the additive contract changes.
