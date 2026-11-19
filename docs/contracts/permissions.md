# CSC Business CRM — Role and Permission Matrix

## 1. Roles

| Role | Description |
|---|---|
| Admin | Full administrative control over users, services, customers, jobs, documents and reports. |
| Employee | Performs assigned operational work, manages permitted customer records and processes documents. |

## 2. Permission Matrix

Legend:
- **Full** — Can create, read, update and manage.
- **Assigned** — Can perform permitted operations on assigned records.
- **Read** — Can view records.
- **No** — No access.
- **Restricted** — Subject to additional authorization and auditing.

The Read-only / Auditor role was removed by owner decision on 2026-11-19. The only roles are `admin` and `employee`.

| Feature | Admin | Employee |
|---|---|---|
| View dashboard | Full | Full |
| View customers | Full | Full |
| Create customers | Full | Full |
| Update customers | Full | Full |
| Delete customers | Restricted | No |
| View services | Full | Read |
| Create services | Full | No |
| Update services | Full | No |
| Deactivate services | Full | No |
| View jobs | Full | Assigned + permitted records |
| Create jobs | Full | Full |
| Update jobs | Full | Assigned |
| Assign employees | Full | No |
| Change job status | Full | Assigned |
| Cancel jobs | Full | No |
| Add job fees | Full | Until a payment is received |
| Edit or delete job fees | Full | No |
| View job history | Full | Assigned |
| Upload documents | Full | Assigned + permitted customer workflows |
| View documents | Full | Authorized records |
| Download documents | Full | Authorized records |
| Archive documents | Full | Restricted |
| Manage OCR results | Full | View processing results |
| View vehicles | Full | Authorized records |
| Create vehicles | Full | Full |
| Update vehicles | Full | Permitted fields |
| Trigger vehicle lookup | Full | Authorized operational workflow |
| View vehicle expiry | Full | Authorized records |
| Create reminders | Full | Full |
| Complete reminders | Full | Assigned / authorized reminders |
| View reminders | Full | Authorized records |
| Generate Excel exports | Full | Restricted to permitted datasets |
| Manage users | Full | No |
| Create employee accounts | Full | No |
| Deactivate users | Full | No |
| Initiate password resets | Full | No |
| View audit logs | Full | No |
| Modify audit logs | No | No |

## 3. Important Authorization Rules

### Admin
- Only administrators can create employee accounts.
- Only administrators can assign and remove employees from jobs.
- Only administrators can manage services and user accounts.
- Sensitive deletion and administrative actions must be audited.

### Employee
- Employees can only modify records permitted by their role and operational assignment.
- Employees can update the status of jobs assigned to them.
- Employees cannot manage users or change role permissions.
- Employee access to personal and vehicle information must be limited to operational requirements.

## 4. Sensitive Data Protection

The following information requires additional protection:
- Aadhaar numbers.
- PAN numbers.
- Engine numbers.
- Chassis numbers.
- Mobile numbers.
- Identity documents.
- Uploaded customer documents.

Rules:
- Mask sensitive values in general-purpose responses.
- Restrict access to full values to authorized workflows.
- Record access to particularly sensitive information.
- Do not expose local file paths or unrestricted storage URLs.
- Do not include sensitive values in application logs unnecessarily.

## 5. Authorization Enforcement

- Permissions must be enforced by the backend.
- Frontend visibility restrictions are not security controls.
- Every protected endpoint must validate the authenticated user and their permissions.
- Unauthorized access returns HTTP `403 Forbidden`.
- Unauthenticated access returns HTTP `401 Unauthorized`.
- Administrative actions must generate audit records.

## 6. Future Changes

Any new role, permission or access restriction must be documented here and approved by the project owner before implementation.