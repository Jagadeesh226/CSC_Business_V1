"""Housekeeping: rows of workers gone for a week are removed (Phase 5.0),
and the daily data clean-up runs (Phase 5.3)."""

from app.repositories import heartbeat_repository
from app.services import cleanup_service
from app.workers import registry

registry.register_maintenance(heartbeat_repository.prune)
registry.register_maintenance(cleanup_service.run_daily)
