"""Registers the once-a-day vehicle expiry check (Phase 4.2) as a worker
maintenance job: every worker loop calls it, and it does its work only on
the first call of each business day."""

from app.services import reminder_service
from app.workers import registry

registry.register_maintenance(reminder_service.run_daily)
