# CSC Business CRM — Architecture Decisions

**Document status:** Approved for Phase 1  
**API version:** `/api/v1`  
**OpenAPI version:** 3.1.0

## 1. Technology Stack

| Layer | Technology |
|---|---|
| Frontend | React + Vite + TypeScript |
| Backend | FastAPI |
| Database | PostgreSQL |
| ORM | SQLAlchemy |
| Database migrations | Alembic |
| Cache and task queue | Redis |
| Background processing | Dedicated Python worker |
| Browser automation | Playwright |
| API documentation | OpenAPI 3.1 |

## 2. API Design

- All business API endpoints use the `/api/v1` prefix.
- Resource IDs use integers.
- API request and response schemas are defined using Pydantic.
- OpenAPI is generated from the FastAPI application.
- PostgreSQL is the source of truth.
- API responses use consistent error structures.
- List endpoints support pagination where appropriate.

## 3. Authentication and User Management

- Authentication uses server-managed sessions.
- Sessions are maintained through HTTP-only cookies.
- Cookies use Secure and appropriate SameSite settings in production.
- CSRF protection is required for state-changing requests.
- Only administrators can create employee accounts.
- Administrators can update, deactivate and reactivate employee accounts.
- Administrators can initiate employee password resets.
- Deactivated users cannot authenticate.
- Passwords must be securely hashed.
- Authentication and administrative actions are audited.

## 4. User Roles

The roles are:
- Admin (stored as `admin`)
- Employee (stored as `employee`)

The Read-only / Auditor role was removed on 2026-11-19. Existing `staff` users migrate to `employee`.

Permissions are defined in `permissions.md`.

## 5. Customer Management

- Employees and administrators can create and search customers.
- Customer records can be updated according to role permissions.
- Sensitive personal information must be protected.
- Sensitive fields must be masked in responses where full values are not required.

## 6. Service Management

- Administrators manage available services.
- Employees can view available services.
- Service changes must not silently alter historical job records.

## 7. Job Management

- Jobs belong to customers and reference services.
- A job can be assigned to multiple employees.
- Job assignments use a many-to-many relationship.
- Assignment and removal actions are audited.
- Job status changes are recorded in status history.
- Until job assignment exists, employees can view all jobs and create jobs, and only admins can edit jobs. After job assignment, employees are limited to their assigned jobs.

Initial job statuses:
- `pending`
- `in_progress`
- `waiting_for_documents`
- `submitted`
- `completed`
- `cancelled`

Allowed status changes:
- `pending` → `in_progress`
- `in_progress` → `waiting_for_documents` or `submitted`
- `waiting_for_documents` → `in_progress`
- `submitted` → `completed`
- Cancelling is a separate admin-only action that needs a reason. It is allowed from any status except `completed` and `cancelled`.
- `completed` and `cancelled` are final. Jobs in these statuses cannot be reopened.

Job fees:
- Employees can add fees only until the job has at least one payment with status `received`.
- Only admins can edit or delete fees.

## 8. Document Management

- Initial file storage uses the local filesystem.
- A storage abstraction must allow future migration to cloud object storage.
- Document binaries are not stored directly in PostgreSQL.
- PostgreSQL stores document metadata and storage references.
- Uploaded files undergo type, size and security validation.
- OCR starts automatically after upload.
- OCR runs asynchronously through the background worker.
- OCR results must include processing status and confidence information where supported.
- Low-confidence or failed OCR results must not silently overwrite verified data.
- Document access and downloads must be permission-controlled.

Document processing statuses:
- `uploaded`
- `processing`
- `completed`
- `failed`

## 9. Vehicle and RC Management

- Employees can upload vehicle RC documents.
- OCR automatically extracts available registration, engine and chassis details.
- Extracted information is used for authorized vehicle-information lookups.
- Vehicle lookup automation uses Playwright.
- VAHAN portal interactions must comply with applicable terms, authorization and access controls.
- CAPTCHA, OTP and other access controls must not be bypassed.
- Insurance and PUCC expiry details are stored with retrieval timestamps.
- Failed or incomplete lookups must be recorded.

## 10. Background Processing

- Redis Streams is used for asynchronous task queuing.
- A dedicated Python worker consumes queued tasks.
- FastAPI does not execute long-running OCR or browser automation synchronously.
- PostgreSQL stores task status and results.
- Tasks must support retries, timeouts and failure handling.
- Duplicate task execution must be handled safely through idempotency.
- Failed tasks must be recoverable and observable.

Initial task types:
- `document_ocr`
- `vehicle_expiry_lookup`
- `vehicle_mobile_lookup`

## 11. Vehicle Expiry Reminders

- Insurance and PUCC expiry dates are monitored.
- Alerts are generated when expiry is within 30 days.
- Already-expired records are separately identified.
- Reminder records maintain their own status and history.
- Reminder statuses:
  - `pending`
  - `ready_to_send`
  - `done`
  - `dismissed`

## 12. WhatsApp Integration

- The system generates WhatsApp click-to-chat links.
- Messages are prefilled but not automatically sent.
- Employees manually review and send messages through WhatsApp.
- Reminder completion is recorded explicitly by the employee.
- The system must not claim a message was delivered merely because a link was opened.

## 13. Excel Exports

- Excel exports are generated from PostgreSQL records.
- Excel files are not the primary data source.
- Exports must respect user permissions.
- Spreadsheet formula injection must be prevented.
- Sensitive fields must be masked or excluded where appropriate.

## 14. Audit Logging

Audit entries reference the affected record by its integer ID (`audit_logs.entity_id`).

Important actions must be recorded, including:
- Authentication events.
- User creation and deactivation.
- Password reset initiation.
- Customer and job changes.
- Job assignments.
- Document uploads and access.
- OCR processing results.
- Vehicle lookups.
- Reminder completion.
- Data exports.

## 15. API Contract Ownership

- `docs/contracts/openapi.json` is generated from FastAPI.
- Only the project owner approves and commits contract changes.
- Frontend TypeScript types are generated from the committed OpenAPI specification.
- Breaking API changes require explicit approval.
- Backend and frontend implementations must follow the approved contract.
