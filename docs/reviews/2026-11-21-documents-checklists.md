# Review: documents and job checklists (Phase 2.1)

- **Branch:** `feat/backend-documents-checklists` at `e3ad959`
- **Diff:** `git diff origin/main...feat/backend-documents-checklists` (backend only: storage, upload validation, models, migration `a7b8c9d0e1f2`, document routes/service/repository, job creation and submit gate, tests)
- **Reviewed against:** `AGENTS.md`, `backend/AGENTS.md`, `docs/contracts/decisions.md` §8, `docs/contracts/permissions.md` (documents rows, §4), the requirements document (§3.2–3.3, FR-06..FR-10, §6, §8, §9 and the security list), previous reviews, and the owner decisions listed in the review request.
- **Reviewer:** review agent. Findings only; no application code was changed.

## Checks run

- `TEST_DATABASE_URL=... uv run pytest -q` (test database `csc_crm_test` only): **384 passed**.
- `bash scripts/check.sh backend` (run on a detached copy of `e3ad959`): dependency lock, Ruff lint, Ruff format, pytest **PASS**; contract check **FAIL (contract drift)**. Expected, because `docs/contracts/openapi.json` has not been updated (see Accepted gaps).
- Probes (test database only, throwaway storage directory and key, all rows truncated afterwards; the database was left at head `a7b8c9d0e1f2`):
  1. Employee E2, assigned only to job B of a customer, uploaded a new version of a **verified** document that is linked only to job A (assigned to E1). Reject by E2: **403**. New version by E2: **201**, document back to `uploaded`, job A's Aadhaar item went from `verified` to `pending` (see M-1).
  2. Anonymous `POST /customers/1/documents` with a 50 MB file part: response **401**, but **52,428,800 bytes** had already been written to the spooled temp file (see M-2).
  3. Single-document checklist item: upload doc 1, reject it, upload doc 2 (allowed), then add a version to doc 1. The item then held **two `uploaded` documents** (`allow_multiple = false`), and both could be verified (see L-1).
  4. Employee on the job: `include_archived=true` lists the archived document and `/download` returns **200** (by design; noted under Checked and correct).
  5. `DOCUMENT_ENCRYPTION_KEY` set to a non-base64 value: upload returned **500**, not the documented 503 (see L-4).
  6. Reuse: employee on job B linked job A's verified Aadhaar, then rejected it: **200**, and job A's item became `rejected` (see Accepted gaps).
  7. Hostile PDFs through `check_upload`: cyclic page tree rejected in 0.01 s; a flat 20,000-page tree (1.5 MB) rejected in 0.27 s; 65,000 pages in an object stream (0.6 MB) rejected in 0.84 s with ~230 MB peak RSS; a 5.5 MB object-stream file rejected in 0.54 s with ~370 MB peak RSS (see L-7).
  8. Migration: downgraded to `f3c7a9e2b1d4`, inserted an `in_progress` job, a `completed` job and a job with no service, plus one active and one inactive requirement; upgraded. Only the open job got a checklist, with the active type only. `service_name` was filled for both jobs with a service and left `NULL` for the other. With one document row present, the downgrade refused with `RuntimeError` and the revision stayed at `a7b8c9d0e1f2`.

## Critical

None.

## High

None.

## Medium

### M-1: Any employee on one of the customer's open jobs can replace (and un-verify) any of that customer's documents

- **File:** `backend/app/services/document_service.py:391-407` (`add_version`), `:98-105` (`_lock_for_write`), `:81-88` (`can_access`)
- **What is wrong:** `add_version` only checks `can_access` (read access) and the closed-job rule. `can_access` is true for every document of a customer while the employee is assigned to *any* open job of that customer. `verify` and `reject` additionally call `_ensure_can_review` (assigned to a job the document is linked to), but replacing does not. Replacing is a stronger change than rejecting: it puts a different file in place and resets the document to `uploaded`, clearing `verified_by`/`verified_at` for every job that uses it.
- **How to trigger:** Customer C has job A (assigned to E1) with a verified Aadhaar, and job B (assigned to E2 only). E2 calls `POST /documents/{id}/versions` with any PNG and a reason. Probe 1: E2's reject is refused with 403, but the new version is accepted with 201, and job A's required item drops from `verified` to `pending`, so job A can no longer be submitted.
- **Impact:** An employee can change the contents of a sensitive identity document they could not verify or reject, on a job they do not work on, and block another employee's job. It is audited (`document_version_added`), but it undercuts the owner rule that review belongs to the assigned employee or an admin. It also allows replacing customer-level documents that are linked to no job at all.
- **Suggested fix:** In `add_version`, call `_ensure_can_review` after `_lock_for_write` (admin, or assigned to a linked job). Optionally, allow replacing an unlinked customer-level document only for its uploader or an admin. Add a permission-denied test matching probe 1.

### M-2: Upload bodies of any size are received and spooled to disk before authentication or the 10 MB check

- **File:** `backend/app/api/v1/routes/documents.py:49-58` and `:139-146` (`_read_limited` runs inside the handler); FastAPI 0.150 `fastapi/routing.py:455` (`await request.form()`) runs before `:511` (`solve_dependencies`)
- **What is wrong:** For a route with `File`/`Form` parameters, FastAPI parses the whole multipart body before it runs the auth dependency. Starlette limits only non-file fields (`max_part_size` 1 MB). File parts are spooled to a temporary file with no limit. `_read_limited` then enforces 10 MB, but only after the full body has been received and written to disk. Nothing in the repository (app middleware, `compose.yaml`, `infra/`) caps the request body.
- **How to trigger:** Probe 2: an unauthenticated POST with a 50 MB file part gets 401, after 50 MB has been written to the temp file. A signed-in user gets 413 the same way. Several parallel multi-GB requests fill the temp disk for as long as they last.
- **Impact:** Anyone who can reach the API can use bandwidth, worker time and temporary disk space with no sign-in. The files are deleted after the request, so the damage is availability only, not data exposure.
- **Suggested fix:** Add a small ASGI middleware (or route-level check) that rejects requests to the two upload paths with 413 when `Content-Length` is above ~10.5 MB, and counts streamed bytes for chunked bodies, before the form is parsed. Also ask the owner to set a matching proxy limit (for example `client_max_body_size 11m`) in the deployment configuration. Add a test showing that an oversized anonymous request is refused without being spooled.

## Low

### L-1: A single-document checklist item can end up with two live documents

- **File:** `backend/app/services/document_service.py:391-404` (`add_version`), `:284-289` (`_occupied`), `:329-333`
- **What is wrong:** The one-document rule is checked only on upload and link. A rejected document frees the item, so a second upload is accepted. Then adding a version to the first (rejected) document moves it back to `uploaded`, and nothing checks the item again.
- **How to trigger:** Probe 3: upload, reject, upload again, add a version to the first document. The item (`allow_multiple = false`) lists two `uploaded` documents, and both can be verified.
- **Impact:** Breaks `allow_multiple = false`. The checklist state is still computed sensibly (verified if either is verified), but staff see two competing documents, and in Phase 2.3 OCR would run on both.
- **Suggested fix:** In `add_version`, for every single-document item the document is linked to (under the job locks already held), refuse with 409 if another live document occupies it. Alternatively, refuse a new upload while a rejected document exists and point to "upload a new version" (which the conflict message already suggests).

### L-2: The closed-job rule for document writes uses the link set read before the document lock

- **File:** `backend/app/services/document_service.py:103-104` (`_lock_for_write`)
- **What is wrong:** `linked_job_ids` is read, then those jobs are locked, then the document is locked. A link that commits between the read and the document lock (for example `link_document` to job J2, and then J2 is completed) is not in `jobs`, so `_ensure_no_closed_job` does not see J2.
- **How to trigger:** Requires timing: the employee's verify/reject/new version waits on the lock of the first linked job while another user links the document to J2 and completes J2. Found by reading the code; not probed. The submit gate itself is not affected: such an interleaving is equivalent to a link, then a submit, then the write, which is a valid order.
- **Impact:** An employee could change a document that a just-closed job relies on. This is rare and audited.
- **Suggested fix:** After `lock_document`, read `linked_job_ids` again. `link_document` also locks the document, so the set is now stable. If it has grown, raise a retryable 409 (or roll back and retry once), rather than locking extra jobs out of order.

### L-3: Orphan files and storage hardening

- **File:** `backend/app/services/document_service.py:259` (`_store`), `backend/app/core/storage.py:61`, `:68-74`
- **What is wrong:** (a) The encrypted file is written before the database commit. If the flush or commit then fails (connection loss, a constraint error), the file stays on disk with no row pointing to it. (b) If the write fails part-way (disk full), the `.tmp` file is not removed. (c) `mkdir(parents=True, mode=0o700)` applies the mode only to the last directory. A missing `DOCUMENT_STORAGE_DIR` root is created with the default umask (usually `0755`).
- **How to trigger:** (a) Kill the database connection after `_store` in an upload. (c) Start with a storage directory that does not exist. Found by reading the code.
- **Impact:** Disk use grows by encrypted, unreferenced files (they stay unreadable without the key). The directory permissions leak only file names and sizes.
- **Suggested fix:** On an exception after `save`, delete the stored file (try/except around the flush and commit, or an after-rollback hook), and later add a periodic sweep for unreferenced keys. Remove `tmp` in a `finally` when `os.replace` did not run. Create the root with `0o700` explicitly, or refuse to start when it is group- or world-readable.

### L-4: A malformed encryption key gives 500 instead of 503

- **File:** `backend/app/core/storage.py:95`
- **What is wrong:** `base64.urlsafe_b64decode` raises `binascii.Error` for invalid input, and that error is not turned into `StorageNotConfigured`.
- **How to trigger:** Probe 5: `DOCUMENT_ENCRYPTION_KEY=not*base64!`, then upload → 500.
- **Impact:** A deployment mistake shows up as an unexplained server error instead of the clear "not configured" 503.
- **Suggested fix:** Use `base64.urlsafe_b64decode(key_b64, validate=True)` inside try/except (`binascii.Error`, `ValueError`) and raise `StorageNotConfigured`. Add a test next to `test_storage_not_configured_is_503`.

### L-5: Metadata views and denied document access are not audited

- **File:** `backend/app/services/document_service.py:175-177` (`get_document`), `:202-224` (`list_customer_documents`), `:91-95` (`_get_accessible`)
- **What is wrong:** Preview, download, upload, version, verify, reject, archive, link and override are all audited. Viewing document details (file names, version history, rejection reasons) is not. Denied attempts (the 404 from `_get_accessible`, the 403 from `_ensure_can_review`) are not either. The requirements list "denied access where appropriate" among the events to audit.
- **How to trigger:** `GET /documents/{id}` as an outsider (404) or as an assignee (200). No audit row is written in either case.
- **Impact:** A probe for other customers' document ids leaves no trace. This is low because the file contents themselves are always audited.
- **Suggested fix:** Audit `document_access_denied` (document id and route only) for record-level denials on document routes, without turning 404 into 403. Whether to audit detail views is an owner decision.

### L-6: No race tests for document writes against the submit gate

- **File:** `backend/tests/test_documents.py:716` (only race test: two uploads to one item), `backend/app/services/document_service.py:103`
- **What is wrong:** The submit gate is safe only because reject, archive, new version and verify lock the linked jobs first. If `_lock_for_write` stopped calling `lock_jobs`, the whole suite would still pass.
- **How to trigger:** Remove `repo.lock_jobs(...)` from `_lock_for_write` and run the tests. Found by reading the tests; this mutation was not run.
- **Impact:** Test gap only. The current code is correct.
- **Suggested fix:** Add a `_run_while_locked`-style test: hold a `submitted` change uncommitted on a job, start `reject` on its only required document in another session, assert that it waits, and that after both commit the order is consistent (the submit happened first, then the document is `rejected`). Add a second test for `archive`.

### L-7: Hostile PDFs cost up to several hundred MB of memory per upload

- **File:** `backend/app/services/file_validation.py:63-72`
- **What is wrong:** `len(reader.pages)` builds the full page list before the 20-page limit is checked. pypdf 7.1.1 has its own limits, and every hostile file in probe 7 was rejected in under 1 s. But a 5.5 MB object-stream file reached ~370 MB peak RSS in the worker thread.
- **How to trigger:** Probe 7 (signed-in user; authentication is checked before `check_upload` runs).
- **Impact:** A few parallel uploads of this kind from a signed-in account could use a small server's memory. Staff accounts only, so the risk is low.
- **Suggested fix:** First read `reader.trailer["/Root"]["/Pages"]["/Count"]` and reject values above 20 before flattening. Keep the full count as the second check. Consider limiting how many uploads run at once (for example a semaphore around `check_upload`).

## Accepted gaps (owner decisions or planned later)

- **Contract drift:** the new routes, the document schemas and `StatusChangeRequest.override_document_check` are not yet in `docs/contracts/openapi.json`. The owner must regenerate and approve the contract (`python scripts/export_openapi.py`).
- **Customer-wide employee access:** an employee on any open job of a customer can read all of that customer's documents, including documents marked `sensitive`. This is the owner's decision. The requirements note that "sensitive identity documents may require stricter permissions", so the owner may want to revisit this for `sensitivity = sensitive` once queues land (2.4).
- **Shared verification state:** a reused document has one status, so the employee on job B can reject it and un-verify job A's item (probe 6). This follows from the decisions "verified documents reusable" and "verify/reject by an employee assigned to a linked job". The owner should confirm it is the intended behaviour.
- **Employees keep read access to documents of closed jobs they were assigned to**, and can list and download archived documents of customers they currently work for (probe 4). This is consistent with "archive only" and "closed jobs are a record".
- **Virus scanning** is deferred to Phase 6. The `processing`/`needs_review` states are reserved for OCR (2.3), and no endpoint sets them yet.
- **Inline PDF preview** under `Content-Security-Policy: sandbox`: some browsers' built-in PDF viewers refuse to render in a sandboxed document. This was not tested here (the browser pane cannot display PDFs). The frontend agent should check this when building the preview, and fall back to download or a JS renderer if needed.

## Checked and correct

- **Record-level access (IDOR):**
  - Every document route resolves the document and runs `can_access` before doing anything, returning 404 to outsiders. This covers detail, download, preview, versions, verify and reject. Archive is admin-only, enforced by the route dependency.
  - `version=` is looked up by `(document_id, version)`, so another document's version cannot be reached.
  - Checklist read and link go through `ensure_job_access` (active assignment).
  - Link requires `document.customer_id == job.customer_id`, read access to the document, a checklist item of that job, a matching type, `verified` status and not archived.
  - Upload with `job_id` requires an active assignment and the same customer. `checklist_item_id` must belong to that job. A customer-level upload by an employee requires an open job of that customer.
  - Listing for an employee without an open job of the customer is restricted to documents of their own jobs.
- **Download headers:**
  - Filenames are reduced by `safe_filename` to `[A-Za-z0-9._ ()-]` before storage, so `Content-Disposition` cannot be broken or injected, and the extra `"` strip is redundant but harmless.
  - `Content-Type` comes from the server-side signature check, never from the client.
  - `nosniff`, `no-store` and `CSP default-src 'none'; sandbox` are set on both preview and download.
  - The session cookie is `SameSite=Lax`, and unsafe methods need the CSRF header, so cross-site form posts to the upload endpoints fail.
- **Crypto:**
  - AES-256-GCM via `cryptography`, with a fresh random 96-bit nonce per file and a new random storage key per version (no nonce reuse at realistic volumes).
  - The storage key is bound as associated data, so swapped or tampered files fail authentication (tested).
  - A format prefix allows later key or format rotation.
  - The key is read from the environment and never logged. The storage key is hex-only with a fixed length, so path traversal is impossible.
  - Files are created `0600` with `O_EXCL`, fsynced and atomically renamed. No storage key or path appears in any response schema.
- **Upload validation:**
  - The signature and the extension must agree, so a PNG named `.pdf` and a PDF named `.exe` are both rejected.
  - Unknown types are refused. Empty files get 422, over 10 MB gets 413.
  - Encrypted PDFs are refused, and so are unparseable PDFs (as 422, never 500).
  - A cyclic page tree is rejected. A missing filename becomes `document` and is then refused for having no matching extension.
  - Polyglots can pass the signature check, but they are only ever served with their detected image or PDF type under `nosniff` and a sandbox CSP.
- **Lifecycle:**
  - Verify is allowed only from `uploaded`/`needs_review`.
  - Reject is allowed from any non-rejected, non-processing state.
  - A new version resets status, verifier and rejection reason, and stores an immutable version row with its reason.
  - Archived documents cannot be verified, rejected or versioned, and they drop out of checklists and the submit gate.
  - Item state priority is verified > pending > rejected > missing.
  - Employees cannot add, replace, verify or reject documents of closed jobs. Admins can.
- **Submit gate:** `change_status` holds the job lock while `build_checklist` runs. Upload and link lock the target job. Verify, reject, archive and new version lock every linked job (in id order) before the document. No code path locks a document and then a job, so I found no deadlock cycle with the existing job → user order. Override is admin-only, needs a reason, is refused when not needed, and is audited with the missing type names. Only `in_progress → submitted` leads to `submitted`, so the gate cannot be bypassed through other transitions.
- **Stale reads under lock:** `lock_document` and `lock_jobs` use `populate_existing`, so status and version numbers are fresh after locking. Two concurrent new versions cannot collide on `uq_document_versions_number`.
- **Audit:** upload, preview, download (with version), version added, verified, rejected (with reason), archived (with reason), linked (with job and item) and override are all recorded. No file contents, hashes, storage keys or OCR values are recorded. The download/preview audit is committed before the bytes are returned.
- **Session use from async routes:** the request body is fully parsed before dependencies run. The DB session is then used by one thread at a time (auth dependency, then `svc.upload`), never concurrently.
- **Queries:** listing loads links, types and users in batches (types per distinct id, which is small). The checklist uses one query for items and one for documents. There is no N+1 per document.
- **Migration** (probe 8): it refuses to run with existing document rows. It backfills `service_name` and checklists for open jobs only, from active requirements of active types. The downgrade refuses while documents exist. A new migration was added and no existing migration was edited.
- **Job creation:** the checklist is copied from active requirements inside the job-creation transaction, and `service_name` is snapshotted.
- **Dependencies:** `cryptography` (AEAD) and `pypdf` (page count and encryption check) are justified. Both are in `uv.lock`.
- **Ownership:** all changes are under `backend/`. IDs are integers throughout.

## Verdict

**CHANGES REQUESTED.** There is no Critical or High finding. M-1 (employees can replace documents they may not review) and M-2 (unbounded upload bodies before sign-in) should be fixed before Phase 2.3 builds on these endpoints. L-1 through L-7 can be fixed in the same pass or tracked.
