import math
import re

from sqlalchemy.orm import Session

from app.core.masking import digits, mask_phone
from app.core.vehicle_numbers import looks_like_registration, normalize
from app.models import Customer, User
from app.repositories import customer_repository as repo
from app.schemas.customer import (
    CustomerCreate,
    CustomerCreated,
    CustomerListResponse,
    CustomerRead,
    CustomerSummary,
    CustomerUpdate,
)
from app.services import audit_service
from app.services.errors import NotFoundError

# permissions.md: admins and employees view, create and update customers.
# Phone numbers are sensitive: masked in lists, shown in full on a customer's
# own record. Audit entries record which fields changed, never the values.


def _summary(customer: Customer) -> CustomerSummary:
    return CustomerSummary(
        id=customer.id,
        name=customer.name,
        phone_masked=mask_phone(customer.phone),
        created_at=customer.created_at,
    )


_PHONE_QUERY = re.compile(r"^\+?[0-9 ()-]+$")


def _parse_search(search: str | None) -> tuple[str | None, str | None]:
    """A query made only of phone characters searches phone numbers; anything
    else searches names. "+91 98765 43210" and "098765 43210" both look for
    9876543210. A phone search needs at least 4 digits."""
    search = (search or "").strip()
    if not search:
        return None, None
    if not _PHONE_QUERY.match(search):
        return search, None
    d = digits(search)
    if search.startswith("+") and d.startswith("91"):
        d = d[2:]
    elif len(d) == 11 and d.startswith("0"):
        d = d[1:]
    return None, d if len(d) >= 4 else None


def get_customer(db: Session, customer_id: int) -> CustomerRead:
    customer = repo.get_customer(db, customer_id)
    if customer is None:
        raise NotFoundError("Customer not found.")
    return CustomerRead.model_validate(customer)


def list_customers(
    db: Session, *, page: int, page_size: int, search: str | None = None
) -> CustomerListResponse:
    name_query, phone_digits = _parse_search(search)
    if search and search.strip() and not name_query and not phone_digits:
        # A phone-style query with fewer than 4 digits matches nothing.
        return CustomerListResponse(
            items=[], total=0, page=page, page_size=page_size, total_pages=0
        )
    customers, total = repo.list_customers(
        db,
        offset=(page - 1) * page_size,
        limit=page_size,
        name_query=name_query,
        phone_digits=phone_digits,
        registration_key=normalize(name_query)
        if name_query and looks_like_registration(name_query)
        else None,
    )
    return CustomerListResponse(
        items=[_summary(c) for c in customers],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size) if total else 0,
    )


def create_customer(db: Session, data: CustomerCreate, actor: User) -> CustomerCreated:
    customer = repo.add_customer(
        db,
        Customer(
            name=data.name,
            phone=data.phone,
            email=str(data.email).lower() if data.email else None,
            address=data.address,
            notes=data.notes,
        ),
    )
    duplicates = repo.find_possible_duplicates(
        db,
        name=customer.name,
        phone_last10=digits(customer.phone)[-10:] or None,
        exclude_id=customer.id,
    )
    audit_service.record(
        db,
        action="customer_created",
        entity_type="customer",
        entity_id=customer.id,
        user_id=actor.id,
        details={"possible_duplicates": [c.id for c in duplicates]}
        if duplicates
        else None,
    )
    db.commit()
    return CustomerCreated(
        customer=CustomerRead.model_validate(customer),
        possible_duplicates=[_summary(c) for c in duplicates],
    )


def update_customer(
    db: Session, customer_id: int, data: CustomerUpdate, actor: User
) -> CustomerRead:
    customer = repo.get_customer(db, customer_id)
    if customer is None:
        raise NotFoundError("Customer not found.")
    changes = data.model_dump(exclude_unset=True)
    if changes.get("email"):
        changes["email"] = str(changes["email"]).lower()
    changed = {k: v for k, v in changes.items() if getattr(customer, k) != v}
    for field, value in changed.items():
        setattr(customer, field, value)
    if changed:
        audit_service.record(
            db,
            action="customer_updated",
            entity_type="customer",
            entity_id=customer.id,
            user_id=actor.id,
            details={"fields": sorted(changed)},
        )
    db.commit()
    return CustomerRead.model_validate(customer)
