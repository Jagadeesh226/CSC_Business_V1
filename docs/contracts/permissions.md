# CSC Business CRM — Role and Permission Matrix

## 1. Roles

| Role | Description |
|---|---|
| Admin | Full administrative control over users, services, customers, jobs, documents and reports. |
| Employee | Performs assigned operational work, manages permitted customer records and processes documents. |
| Read-only / Auditor | Views authorized records and reports without modifying business data. |

## 2. Permission Matrix

Legend:
- **Full** — Can create, read, update and manage.
- **Assigned** — Can perform permitted operations on assigned records.
- **Read** — Can view records.
- **No** — No access.
- **Restricted** — Subject to additional authorization and auditing.

| Feature | Admin | Employee | Read-only / Auditor |
|---|---|---|---|
| View dashboard | Full | Full | Read |
| View customers | Full | Full | Read |
| Create customers | Full | Full | No |
| Update customers | Full | Full | No |
| Delete customers | Restricted | No | No |
| View services | Full | Read | Read |
| Create services | Full | No | No |
| Update services | Full | No | No |
| Deactivate services | Full | No | No |
| View jobs | Full | Assigned + permitted records | Read |
| Create jobs | Full | Full | No |
| Update jobs | Full | Assigned | No |
| Assign employees | Full | No | No |
| Change job status | Full | Assigned | No |
| View job history | Full | Assigned | Read |
| Upload documents | Full | Assigned + permitted customer workflows | No |
| View documents | Full | Authorized records | Read |
| Download documents | Full | Authorized records | Read |
| Archive documents | Full | Restricted | No |
| Manage OCR results | Full | View processing results | Read |
| View vehicles | Full | Authorized records | Read |
| Create vehicles | Full | Full | No |
| Update vehicles | Full | Permitted fields | No |
| Trigger vehicle lookup | Full | Authorized operational workflow | No |
| View vehicle expiry | Full | Authorized records | Read |
| Create reminders | Full | Full | No |
| Complete reminders | Full | Assigned / authorized reminders | No |
| View reminders | Full | Authorized records | Read |
| Generate Excel exports | Full | Restricted to permitted datasets | Read-only reports |
| Manage users | Full | No | No |
| Create employee accounts | Full | No | No |
| Deactivate users | Full | No | No |
| Initiate password resets | Full | No | No |
| View audit logs | Full | No | Restricted read |
| Modify audit logs | No | No | No |

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

### Read-only / Auditor
- Cannot create, update, delete, upload or modify business records.
- Can only view records permitted by their access scope.
- Audit-log access is read-only.

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