"""Housekeeping for monitoring (Phase 5.0): rows of workers gone for a
week are removed, so the heartbeat table stays small."""

from app.repositories import heartbeat_repository
from app.workers import registry

registry.register_maintenance(heartbeat_repository.prune)
