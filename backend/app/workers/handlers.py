"""Imports every module that registers task handlers.

The worker imports this at start-up.
"""

from app.workers import (
    export_handler,  # noqa: F401 - registers report_export
    lookup_handler,  # noqa: F401 - registers vehicle lookups
    ocr_handler,  # noqa: F401 - registers document_ocr
    ops_jobs,  # noqa: F401 - heartbeat housekeeping
    reminder_jobs,  # noqa: F401 - daily expiry check
)
