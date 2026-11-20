# Review: customers API and service catalogue with document types (Phase 2.0)

- **Branch:** `feat/backend-customers-catalogue` at `c7ef617`
- **Diff:** `git diff origin/main...feat/backend-customers-catalogue` (backend only: customers and catalogue routes, schemas, services and repositories, `DocumentType` model, `ServiceDocumentRequirement` change, `core/masking.py`, migration `f3c7a9e2b1d4`, tests)
- **Reviewed against:** `AGENTS.md`, `backend/AGENTS.md`, `docs/contracts/decisions.md` §5, §6 and §14, `docs/contracts/permissions.md` (customers, services, §4 sensitive data including mobile numbers), requirements §3.2–3.3, §5.1, §5.3, §9 and §10, earlier reviews, and the owner decisions in the review request.
- **Reviewer:** review agent. Findings only; no application code was changed.

## Checks run

- `TEST_DATABASE_URL=... uv run pytest -q` (test database `csc_crm_test` only): **326 passed**.
- `bash scripts/check.sh backend` (with `TEST_DATABASE_URL` set to the test database): pytest **PASS**. Contract check **FAIL (contract drift)**. This is expected because the owner has not yet updated `docs/contracts/openapi.json` (see Accepted gaps).
- `alembic check` against the test database at head: "No new upgrade operations detected." (Models and migrations agree.)
- Probes (scratch pytest files loaded with the branch's `tests/conftest.py`, test database only, each test rolled back; migration probes used rows with ids 900001+ and deleted them afterwards). At the end every table checked (customers, services, categories, document types, requirements, jobs, users, audit logs) was empty and the database was back at head `f3c7a9e2b1d4`.
  1. **Category deactivation:** after `PATCH /service-categories/{id} {"is_active": false}`, `GET /services?active=true` hides the service, but `POST /jobs` with that service returns **201**. Deactivating and then reactivating the service inside the inactive category returns **200** (see M-1).
  2. **Uniqueness races:** with `code_taken`, `name_taken` and `requirement_exists` patched to `False` (the state of a second request that ran its check before the first one committed), a duplicate category code, category name, service code, document type code and requirement each raise **`IntegrityError` out of the request (500)**. Only the category rename path returns 409 (see L-1).
  3. **Inactive document type:** after deactivating a requirement and then its document type, re-activating the requirement returns **200**. `GET .../document-requirements?active=true` then lists it with the inactive type. Re-adding the same type returns 422 "inactive document type", not 409 (see L-2).
  4. **Requirement audit:** `service_requirement_added` is stored as `entity_type="service"`, `details={"fields": ["document_type:PAN"]}`; `service_requirement_updated` as `{"fields": ["is_required"]}`. Neither has the requirement id (see L-3).
  5. **Migration:**
     - `aadhaar` and `AADHAAR` on the same service (allowed by the old case-sensitive unique constraint): upgrade fails with `UniqueViolation` on `uq_service_document_requirement_type`. The transaction rolls back and the database stays at `e1a2b3c4d5f6`.
     - A 60-character code fails with `value too long for type character varying(50)`. A 160-character name fails with `character varying(150)`.
     - Normal data: `aadhaar` (service 1, name "Aadhaar") and `Aadhaar` (service 2, name "Aadhaar card (front)") become one type `AADHAAR` named "Aadhaar". A code with a space, `rc book`, becomes `RC BOOK` (not a valid API code). `is_required` and `allow_multiple` are kept.
     - Downgrade restores `AADHAAR`/"Aadhaar" for both rows: the original case and the second name are lost (see L-4).
  6. **Phone handling:** `+91-98765-43210`, `(0) 98765 43210` and `-----9876543210` are accepted and stored as typed. `0091 9876543210` (14 digits) is rejected. Searching `+91 9876` does **not** find a customer stored as `9876543210`. Searching `Ravi 98765` returns Ravi and also Meena (`9000098765`), because the phone match is OR-ed with the name match. `%`, `_` and `\` searches match nothing (LIKE escaping works) (see L-5).
  7. **Duplicates:** `919876543210` and `09876543210` are flagged as the same number (last 10 digits). Duplicate hints show only `phone_masked`.
  8. **Service rename:** after `PATCH /services/{id} {"name": "Renamed", "fee_type": "variable", "default_service_fee": "999"}`, an existing job's `service` shows `"name": "Renamed"`. Its fee line keeps the old description "RC" and amount 100.00 (see L-6).
  9. **Category names:** `Transport` and `transport` are both created (201). `Transport ` (trailing space) is stripped and returns 409 (see L-7).
  10. **NUL bytes:** `POST /customers {"name": "a\u0000b"}`, `GET /customers?search=a%00`, `POST /service-categories` with a NUL in the name, and the existing `POST /jobs` with a NUL in the title all raise `DataError` (500) (see L-8).
  11. **Fee rules on update:** variable→fixed without a fee: 422. Clearing the fee of a fixed service: 422. Moving to a missing category: 404. Changing `code`: 422 (field not allowed). A fixed service with `default_service_fee: 0` is accepted, and the job gets no zero fee line (the earlier fees L-2 is handled).
  12. **Schemas:** `_NoNulls` private `_nullable` works: `CategoryUpdate(description=None)` and `ServiceUpdate(default_service_fee=None)` are accepted, `CategoryUpdate(name=None)` is rejected. Duplicate OCR keys and 31 OCR fields give 422.

## Critical

None.

## High

None.

## Medium

### M-1: Deactivating a category hides its services from the dropdown, but jobs can still be created for them, and they can be reactivated inside it

- **File:** `backend/app/services/job_service.py:96` (checks only `service.is_active`), `backend/app/services/catalogue_service.py:164-183` (`update_service`: the category is checked only when `category_id` changes, not when `is_active` becomes `true`), `backend/app/repositories/catalogue_repository.py:64-66` (the "active = service and category" rule lives only in the list query)
- **What is wrong:** This change defines an active service as "the service and its category are active" (repository comment, `GET /services?active=true`), and `create_service` rejects an inactive category. The same rule is not applied where it matters: `create_job` accepts any active service, whatever its category, and `update_service` allows `is_active: true` on a service whose category is inactive.
- **How to trigger:** Probe 1. Deactivate a category that has an active service, then `POST /api/v1/jobs` with that service: 201. Or deactivate the service, then `PATCH /services/{id} {"is_active": true}`: 200.
- **Impact:** An admin who retires a whole category (for example a department that stopped a scheme) believes its services are unavailable, but stale forms, a bookmarked page or any API client can still create jobs for them. Each job also gets the service's default fee. The upcoming checklist step will copy requirements from such services as well. This is a business-rule gap, not a security issue.
- **Suggested fix:** Put the rule in one place, for example `catalogue_service.ensure_service_usable(db, service)` (service active **and** category active), and call it from `create_job`. In `update_service`, run `_check_category` when the result is `is_active=True` (not only when `category_id` changes). Add tests for both paths. (Whether deactivating a category should also deactivate its services is an owner choice; the check above is needed either way.)

## Low

### L-1: Uniqueness races return 500, because `repo.add` flushes before `_commit_unique` can catch the error

- **File:** `backend/app/repositories/catalogue_repository.py:16-19` (`add` calls `db.flush()`), `backend/app/services/catalogue_service.py:55-60` (`_commit_unique` wraps only `db.commit()`), used at `:84`, `:160`, `:203`, `:275`
- **What is wrong:** The unique index violation is raised by the `INSERT` at `flush()`, inside `repo.add`, before the `try` in `_commit_unique`. For `create_category`, `create_service`, `create_document_type` and `add_requirement`, the `except IntegrityError` branch is dead code. Only `update_category` (no flush before commit) gets the intended 409.
- **How to trigger:** Two admins create the same category code, service code, document type code or service requirement at the same time. The second request passes the `*_taken` / `requirement_exists` check, then blocks on the unique index until the first commits, then fails. Probe 2 reproduces this deterministically by patching the checks.
- **Impact:** A generic 500 instead of a clear 409. No duplicate rows are created and no details leak (the database constraints hold). Admin-only and rare.
- **Suggested fix:** Wrap both the add/flush and the commit, for example make `_commit_unique` a context manager around `repo.add(...)` through `db.commit()`, or catch `IntegrityError` in `repo.add`'s callers. Add one test per create path that patches the pre-check to `False` and expects 409 (this would have caught it).

### L-2: Requirements whose document type is inactive still show as active, and can be re-activated

- **File:** `backend/app/services/catalogue_service.py:279-293` (`update_requirement`), `:251-262` (`add_requirement` order of checks), `backend/app/repositories/catalogue_repository.py:94-100` (`list_requirements`)
- **What is wrong:** `add_requirement` refuses an inactive document type, but:
  - deactivating a document type leaves every requirement that uses it active, and `?active=true` still returns them;
  - `update_requirement` lets an admin set `is_active: true` on a requirement whose type is inactive;
  - re-adding a type that the service already lists returns 422 "inactive document type" instead of 409 "already lists", because the inactive check runs first.
- **How to trigger:** Probe 3.
- **Impact:** Today only the admin screen is affected. In the next step, job checklists are copied from "active requirements" at job creation. If that copy uses this list, new jobs will require a document type that admins retired. This is the same "active means parent active too" question as M-1.
- **Suggested fix:** Decide the rule now, before the checklist step: either filter `DocumentType.is_active` in the `active=true` list (as services do with categories) or refuse to deactivate a type that active requirements use. Apply the same check in `update_requirement` when activating. Run the duplicate check before the inactive check in `add_requirement`.

### L-3: Requirement audit rows do not identify the requirement, and misuse the `fields` key

- **File:** `backend/app/services/catalogue_service.py:266-274` (`service_requirement_added`), `:290-291` (`service_requirement_updated`)
- **What is wrong:** Both rows use `entity_type="service"`, `entity_id=service_id`. The added row stores the document type as `{"fields": ["document_type:PAN"]}` (not a field list). The updated row stores only the changed field names. Neither records the requirement id or, for updates, which document type it was.
- **How to trigger:** Probe 4. Add two requirements to one service, then update one of them. The audit cannot tell which one changed.
- **Impact:** The audit trail for "which documents a service requires" is ambiguous. This matters once checklists are frozen from these rows and someone asks why a job required a document.
- **Suggested fix:** Use `details={"requirement_id": req.id, "document_type_id": ..., "document_type_code": ...}` on add, and the same plus `"fields": changed` on update. Assert these in tests.

### L-4: The migration aborts on case-colliding or overlong legacy codes, and its downgrade loses the original codes and names

- **File:** `backend/migrations/versions/f3c7a9e2b1d4_add_document_types.py:69-77` (insert), `:106-109` (new unique constraint), `:120-127` (downgrade)
- **What is wrong:**
  - The old unique constraint `(service_id, document_code)` was case-sensitive, so one service could hold `aadhaar` and `AADHAAR`. Both map to one type, and creating `uq_service_document_requirement_type` fails.
  - Old columns were `VARCHAR(100)` / `VARCHAR(200)`. The new type columns are `VARCHAR(50)` / `VARCHAR(150)`, so a longer code or name fails the insert.
  - When the same code had different names on different services, only the first name is kept. The others are not copied anywhere (for example into the requirement's `description`).
  - Codes with characters the API rejects (spaces, dots) are converted as-is, for example `RC BOOK`.
  - Downgrade writes back the upper-cased code and the shared name, not the originals.
- **How to trigger:** Probe 5.
- **Impact:** Low in practice. No API ever wrote these rows (requirements had no API before this change), and Postgres rolls the failed upgrade back cleanly. If a database has hand-entered rows, the deploy stops with a constraint error, or names are silently merged.
- **Suggested fix:** Before converting, check for these cases (`GROUP BY service_id, upper(document_code) HAVING count(*) > 1`, `length(document_code) > 50`, `length(document_name) > 150`) and raise a clear error naming the rows, or merge the duplicate requirements explicitly. Copy a differing `document_name` into `description` when it is empty. State in the docstring that the downgrade is lossy for code case and names.

### L-5: Phone numbers are stored as typed and matched only by substring, so searching with a country code misses customers

- **File:** `backend/app/schemas/customer.py:21-28` (`_PHONE_CHARS`, `_check_phone`), `backend/app/repositories/customer_repository.py:17-18, 31-32` (`_phone_digits`, digit `LIKE`)
- **What is wrong:**
  - The validator accepts any mix of `+`, digits, spaces, brackets and hyphens with 10–13 digits, including `-----9876543210` and `(0) 98765 43210`. The value is stored unchanged.
  - Search compares the digits of the query as a substring of the digits of the stored phone. A query in a different format from the stored one fails: `+91 9876` (digits `919876`) does not find `9876543210`. Duplicate detection does handle this (last 10 digits), so search and duplicate detection disagree.
  - A query that mixes a name and digits OR-s the phone match, so `Ravi 98765` returns every customer whose phone contains `98765`.
  - There is no normalized, indexed phone column. Every phone search runs `regexp_replace` on every row. Requirements §9.1 asks to "index normalized mobile", and §5.1 to "normalize phone numbers for matching".
- **How to trigger:** Probe 6.
- **Impact:** Requirements §3.2 say staff "enter mobile number first" to find the customer. Typing the number with `+91` returns no match, so staff are likely to create a duplicate customer (the duplicate hint will then flag it). Full table scans are fine at CSC scale today.
- **Suggested fix:** Normalize on write: store a `phone_normalized` column (last 10 digits for Indian mobiles, or E.164) with an index, keep `phone` as the display value. Normalize the search digits the same way (drop a leading `91`/`0` when more than 10 digits). Optionally tighten the format to one leading `+` and single separators.

### L-6: Renaming a service changes the service name shown on existing jobs

- **File:** `backend/app/services/job_service.py:70` (`ServiceRef` built from the live `services` row), `backend/app/services/catalogue_service.py:164-183` (`update_service` allows renaming)
- **What is wrong:** Jobs store only `service_id`. Before this change no API could rename a service, so this did not matter. Now an admin rename is shown on every old job and in job lists. Fees are not affected (they are copied at job creation, probe 8).
- **How to trigger:** Probe 8.
- **Impact:** `decisions.md` §6 says "Service changes must not silently alter historical job records". A rename (for example "RC Renewal" → "RC Renewal (online)") makes old jobs and future receipts or exports show a name that did not exist when the work was done. The `code` cannot change, so the link itself is stable.
- **Suggested fix:** Owner decision. Either accept that the live name is shown (and document it), or snapshot `service_name` on the job at creation (with the receipt label later, requirements §5.3). The audit `service_updated` already records that `name` changed.

### L-7: Category names are unique only case-sensitively

- **File:** `backend/app/repositories/catalogue_repository.py:29-33` (`name_taken` uses `==`), `backend/app/models/service_category.py` (`name` unique, case-sensitive)
- **What is wrong:** `Transport` and `transport` can both exist. Codes are upper-cased and so are safe; names are not normalized.
- **How to trigger:** Probe 9.
- **Impact:** Two near-identical groups in the category-grouped service dropdown. Cosmetic.
- **Suggested fix:** Compare with `func.lower(model.name) == name.lower()` and add a unique index on `lower(name)` in a new migration.

### L-8: NUL characters in text input cause a 500 (pre-existing, now also on customer and catalogue endpoints)

- **File:** `backend/app/schemas/customer.py` (`CustomerName`, `Notes`, `search` query), `backend/app/schemas/catalogue.py` (`Name150`, `Name200`, `Text5000`); the same applies to existing job fields
- **What is wrong:** PostgreSQL text cannot contain `\x00`. Pydantic accepts it, and psycopg raises `DataError`, which becomes a 500.
- **How to trigger:** Probe 10, for example `GET /api/v1/customers?search=a%00`.
- **Impact:** A generic 500 for bad input instead of 422. No data is changed and no details leak. The same behaviour already exists for job titles, so this is not introduced here, but this change adds a `GET` search that any signed-in user can hit.
- **Suggested fix:** One shared string validator that rejects control characters (at least `\x00`) for all text fields and the search parameter, or a global `DataError` handler that returns 422. Best done once across the backend.

### L-9: Test gaps

- **File:** `backend/tests/test_catalogue_api.py`, `backend/tests/test_customers_api.py`
- **What is wrong:**
  - No test for the IntegrityError paths (would have caught L-1).
  - No test that a job cannot be created for a service in an inactive category, or that such a service cannot be reactivated (M-1).
  - No test for inactive document types in requirement lists (L-2).
  - No test of the requirement audit details (L-3), or of `service_category_updated` / `document_type_updated` audit rows.
  - No migration test for the data conversion (L-4).
  - Phone search tests use the same format as stored; none uses a `+91` prefix (L-5).
- **How to trigger:** Read the tests; probes 1–6 show the behaviour they would cover.
- **Impact:** Test gap only.
- **Suggested fix:** Add the tests listed with the fixes above.

## Accepted gaps (owner decisions, not findings)

- **Contract:** `docs/contracts/openapi.json` has none of the 4 customer routes or the 13 catalogue routes, so `check.sh` reports contract drift. Owner PR pending. All changes are additive (no existing route or field changes). Notes for that PR:
  - Requirements §10 names the search parameter `query` (`GET /api/v1/customers?query=`). This branch uses `search`. Choose one before the frontend is built.
  - `PATCH .../document-requirements/{id}` and `PATCH /document-types/{id}` declare only 404, not 422.
  - `default_service_fee` is a decimal string in responses, as with fees.
- **Search scope:** requirements §3.3 also list Customer ID, Job ID and vehicle registration as search keys. There is no `customer_code` yet (requirements §9) and the number is not searched. This is fine for this step, but the gap should be tracked.
- **Phone exposure:** phones are masked in lists, search results and duplicate hints, and full on `GET /customers/{id}`, as the owner decided. Job responses already expose the full phone (earlier owner decision). Searching by 4+ digits lets an employee find customers by partial number. Employees already have full customer access (permissions.md), so this gives nothing beyond `GET /{id}`. Owner question: permissions.md §4 says "record access to particularly sensitive information" and lists mobile numbers. Full-phone reads (`GET /customers/{id}`) are not audited. Decide whether mobile numbers count as "particularly sensitive" (Aadhaar and PAN clearly will in the document steps).
- **Duplicate hints:** checked only on create, not on phone or name change, and not race-safe (two simultaneous creates do not see each other). Customers are still created. This is acceptable for a hint; requirements §3.3 leave merge to an admin.
- **Catalogue lists are unbounded:** categories, services, document types and requirements are returned without pagination. A CSC catalogue is tens to a few hundred rows, and the dropdown needs the full list. Acceptable.
- **No customer delete or archive** in this step (owner decision). Requirements §9 mention `archived_at` later.

## Fitness of the document type design for the next steps (notes, not findings)

- `code` cannot be changed after creation, which makes it a safe stable key for checklists and OCR results. Good.
- `ocr_fields` and `sensitivity` can be edited at any time. OCR results and frozen job checklists should store the field definitions (or at least the keys and the `sensitive` flags) they were made with, so a later edit does not change how old results are shown or masked.
- `sensitivity` on the type and `sensitive` on each field are independent (a `normal` type may have a `sensitive` field, probe 12). Define which one controls masking and access auditing for documents; probably "either is sensitive".
- OCR fields have no data type (date, number, identifier) or target (which customer, job or vehicle field a verified value goes to). The OCR review step will need both to apply "OCR must not silently overwrite verified customer data" field by field.
- Requirements can say required/optional and `allow_multiple`, but not "one of" groups (for example Aadhaar **or** voter ID as address proof). Confirm with the owner whether CSC services need this before checklists are built.
- Allowed file types and size limits are not per document type. They can stay global (requirements §6) unless the owner wants, for example, photos only for a passport photo type.

## Checked and correct

- **Authorization:**
  - Every new route declares `CurrentUser` or `AdminUser`. All 8 catalogue write routes return 403 "Admin access required." for an employee and 401 when signed out, even for non-existent ids (no existence leak, tested). Catalogue reads require sign-in (tested).
  - Customer routes allow admins and employees for view, create and update, as permissions.md says (employee create and update tested). There is no delete route.
  - Writes need CSRF (customer write tested; catalogue writes use the same session dependency).
  - Path ids are bounded to int4 (`2**31` gives 422).
- **Mass assignment:** every create and update schema has `extra="forbid"`. `id`, `created_at`, `code` on update, and `is_active` on create are rejected (tested for customers, probe 11 for service `code`). Requirements are created with `service_id` from the path, not the body. A requirement from another service gives 404 (tested).
- **Search injection:** the name match uses `ilike` with `\`, `%` and `_` escaped and `escape="\\"`. The phone match uses only digits from `\D`-stripping, so it cannot contain wildcards. Everything is bound parameters. `%`, `_` and `\` searches return nothing (probe 6). The search is limited to 200 characters, page size to 100.
- **Masking:** `mask_phone` shows only the last 4 digits. Lists, search results and duplicate hints use `CustomerSummary` with `phone_masked` and no `phone` field (tested: `"43210"` not in the list item).
- **Audit without personal data:** `customer_created` stores only duplicate ids. `customer_updated` stores only the sorted names of changed fields (tested that the address value is absent). Catalogue audits store field names only. Unchanged `PATCH` writes no audit. All audits use the integer entity id and the acting user.
- **Duplicate detection:** same name (case-insensitive, after trimming) or the same last 10 digits of the phone, excluding the new customer, at most 10, ordered by id. Different formats of one number match (probe 7). Customers without a phone match only by name.
- **Validation:**
  - Customer name 1–200 characters, trimmed; `name: null` is 422. Email uses `EmailStr` (already a dependency) and is stored lower-case. Phone 10–13 digits; null clears it.
  - Codes are trimmed, upper-cased and checked against `^[A-Z0-9_-]{2,50}$`. Names and texts match the column lengths.
  - OCR fields: key pattern `^[a-z][a-z0-9_]{0,49}$`, unique keys, at most 30, label 1–100 characters (probes 12).
  - Fee rules: create requires a fee for fixed services. Update checks the resulting `fee_type` and fee together, so switching variable→fixed without a fee and clearing a fixed fee both give 422 (probe 11). The database check constraints are a second guard. Fees are `Decimal`, quantized to 2 places, `>= 0`.
  - `_NoNulls` correctly allows `null` only for the listed nullable fields (probe 12).
- **Existing jobs:** changing a service's fee type or default fee does not change existing job fees (probe 8). Only the displayed name changes (L-6). Inactive services are already refused for new jobs.
- **Migration:** single new revision on head `e1a2b3c4d5f6`. Named constraints (`fk_service_document_requirements_document_type_id`, `uq_service_document_requirement_type`, `ck_document_types_sensitivity`) match the models. The new column is filled before `NOT NULL`. Structurally reversible (probe 5). `alembic check` is clean. Data-edge cases are L-4.
- **Queries:** customer list is one count plus one page query. Service and requirement lists use a single join each (no N+1). `get_service` after write re-reads with the join.
- **Errors:** business rule failures give 422, missing records 404, known duplicates 409, all with the standard `{"detail": ...}` shape. The 500 cases are L-1 and L-8.
- **AGENTS.md:** changes only under `backend/`. No contract edits, no new dependencies, integer ids throughout. Tests use fake customer data. `ruff`, format and the full test suite pass. Scope matches step 2.0 (no document upload, checklist or OCR code).

## Verdict

**CHANGES REQUESTED.** There are no Critical or High findings. M-1 breaks the "active" rule that this change itself defines (a deactivated category's services can still get new jobs and can be reactivated), and the next step (checklists copied at job creation) builds on that rule. It is a small fix. L-1 and L-2 should be fixed in the same pass, because they are small and L-2 decides how checklists treat retired document types. L-6 needs an owner decision. The other Low findings can be follow-ups.
